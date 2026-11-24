"""US-089: the Volume Watch study (V1 to V9). Read-only on the store.

Run from the MAIN checkout (the data is there), this file from the US-089 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/volume_study.py" <stage> [<stage> ...]

Stages
  load    one-minute bars of the 18 desk stocks, the indices and INDIAVIX from
          `data/onelot.db` (SQLite URI mode=ro), up to 2026-11-07, turned into day x minute
          matrices cached as OUT/cache/<series>.npz. At most 2 worker processes.
  study   V1 to V9 on the cached matrices; tables to OUT/report.txt, numbers to
          OUT/numbers.json, the facts for `/api/volume/study` to OUT/evidence.json.

The indicator (`src/onelot/volume/indicator.py`) is loaded by FILE PATH from this worktree;
the worktree's `src` is never on the path. Outputs go to data/research/volume_2026-11-24/.

Fixed before any result (RESEARCH-RULES 2, 4, 5, 6):
* periods TUNE to 2024-11-07, CHOOSE to 2025-11-07, TEST to 2026-11-07; numbers in that order;
* the up-share rule: the highest TUNE rank correlation of the basket's 5-minute flow share
  with NIFTY's return over the same 5 minutes; within 0.01 of each other, the higher
  correlation with the NEXT 5 minutes wins;
* market level: of 2.5, 3.0, 3.5 the one whose TUNE alerts a day (15-minute cooldown) lie in
  1 to 3, closest to 2; stock level: the lowest of 3.5, 4.0, 4.5 with at most 6 a day;
* t statistics treat one session as one draw; an option buyer needs 5 to 10 basis points of
  the index in the right direction to cover costs.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

WT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ind = _load("volume_indicator_us089", WT / "src" / "onelot" / "volume" / "indicator.py")
# the harness of the MAIN checkout (PYTHONPATH=scripts), never this worktree's copy beside us
sys.path = [p for p in sys.path if Path(p or ".").resolve() != WT / "scripts"]
import mtf_bruteforce as h  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

OUT = h.ROOT / "data" / "research" / "volume_2026-11-24"
CACHE = OUT / "cache"
DB = h.ROOT / "data" / "onelot.db"
STOCKS = list(h.STOCKS)
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
VIX = "INDIAVIX"
BANKS = {"HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "BAJFINANCE"}
END = dt.date(2026, 11, 7)
PERIODS = {
    "TUNE": (dt.date(2000, 1, 1), dt.date(2024, 11, 7)),
    "CHOOSE": (dt.date(2024, 11, 8), dt.date(2025, 11, 7)),
    "TEST": (dt.date(2025, 11, 8), dt.date(2026, 11, 7)),
}
NAMES = ("TUNE", "CHOOSE", "TEST")
MARKET_LEVELS = (2.5, 3.0, 3.5)
STOCK_LEVELS = (3.5, 4.0, 4.5)
COOLDOWN = 15
CAP = 12
WORKERS = 2
N = ind.SESSION_MINUTES


# ───────────────────────────── stage: load ──────────────────────────────
def _read(key: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE series_key = ? AND "
            "resolution = '1m' AND ts < ? ORDER BY ts",
            (key, (END + dt.timedelta(days=1)).isoformat()),
        ).fetchall()
    finally:
        con.close()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    idx = pd.to_datetime(df.pop("ts"), format="ISO8601", utc=True).dt.tz_convert(ind.IST)
    df.index = pd.DatetimeIndex(idx)
    df = df[~df.index.duplicated(keep="last")].astype(float).fillna(0.0)
    return df[(df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]


def _load_one(key: str) -> str:
    t0 = time.time()
    df = _read(key)
    out: dict[str, Any] = {}
    close = ind.day_matrix(df["close"])
    out["days"] = close.index.to_numpy().astype("datetime64[D]").astype(str)
    out["close"] = close.to_numpy()
    out["high"] = ind.day_matrix(df["high"]).reindex(close.index).to_numpy()
    out["low"] = ind.day_matrix(df["low"]).reindex(close.index).to_numpy()
    if key in STOCKS:
        f = ind.flows(df, "clv")
        out["turn"] = ind.day_matrix(f["turnover"]).reindex(close.index).to_numpy()
        for rule in ind.RULES:
            buy = f["turnover"] * ind.up_share(df, rule)
            out[f"buy_{rule}"] = ind.day_matrix(buy).reindex(close.index).to_numpy()
    np.savez_compressed(CACHE / f"{key}.npz", **out)
    return f"{key}: {len(df)} bars, {len(close)} sessions, {time.time() - t0:.1f}s"


def stage_load() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    keys = STOCKS + INDICES + [VIX]
    with ProcessPoolExecutor(WORKERS) as ex:
        for line in ex.map(_load_one, keys):
            print(line, flush=True)


# ───────────────────────────── helpers ──────────────────────────────
class Data:
    """Every cached matrix on one session index."""

    def __init__(self) -> None:
        raw = {k: dict(np.load(CACHE / f"{k}.npz")) for k in STOCKS + INDICES + [VIX]}
        days = sorted(set().union(*(set(raw[k]["days"]) for k in STOCKS)))
        self.days = pd.DatetimeIndex(pd.to_datetime(days))
        self.dates = np.array([d.date() for d in self.days])
        self.m: dict[str, dict[str, np.ndarray]] = {}
        for k, r in raw.items():
            src = pd.DatetimeIndex(pd.to_datetime(r["days"]))
            pos = src.get_indexer(self.days)
            self.m[k] = {}
            for name, a in r.items():
                if name == "days":
                    continue
                out = np.full((len(self.days), N), np.nan)
                ok = pos >= 0
                out[ok] = a[pos[ok]]
                self.m[k][name] = out
        self.period = np.array(["" for _ in self.dates], dtype=object)
        for name, (a, b) in PERIODS.items():
            self.period[(self.dates >= a) & (self.dates <= b)] = name

    def frame(self, a: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(a, index=self.days, columns=range(N))


def ffill_rows(a: np.ndarray) -> np.ndarray:
    """Forward fill along minutes inside each session."""
    return pd.DataFrame(a).ffill(axis=1).to_numpy()


def ret_ahead(c: np.ndarray, cols: np.ndarray, rows: np.ndarray, hz: int) -> np.ndarray:
    """close[m + hz] / close[m] - 1 on a forward-filled close; NaN past the session."""
    tgt = cols + hz
    ok = tgt < N
    out = np.full(len(cols), np.nan)
    out[ok] = c[rows[ok], tgt[ok]] / c[rows[ok], cols[ok]] - 1.0
    return out


def ret_back(c: np.ndarray, cols: np.ndarray, rows: np.ndarray, hz: int) -> np.ndarray:
    src = cols - hz
    ok = src >= 0
    out = np.full(len(cols), np.nan)
    out[ok] = c[rows[ok], cols[ok]] / c[rows[ok], src[ok]] - 1.0
    return out


def clustered(values: np.ndarray, rows: np.ndarray) -> tuple[float, float, int, int]:
    """Mean of `values`, its t with one session as one draw, n values, n sessions."""
    ok = np.isfinite(values)
    v, r = values[ok], rows[ok]
    if len(v) < 2:
        return float("nan"), float("nan"), int(len(v)), int(len(set(r)))
    s = pd.Series(v).groupby(r).mean()
    if len(s) < 2 or s.std(ddof=1) == 0:
        return float(v.mean()), float("nan"), int(len(v)), int(len(s))
    return float(v.mean()), float(s.mean() / s.std(ddof=1) * np.sqrt(len(s))), len(v), len(s)


def ci_by_day(values: np.ndarray, rows: np.ndarray) -> tuple[float, float, float, int]:
    """Mean, 95% interval from day means, n values."""
    ok = np.isfinite(values)
    v, r = values[ok], rows[ok]
    if len(v) < 3:
        return float("nan"), float("nan"), float("nan"), int(len(v))
    s = pd.Series(v).groupby(r).mean()
    se = s.std(ddof=1) / np.sqrt(len(s)) if len(s) > 1 else float("nan")
    m = float(v.mean())
    return m, m - 1.96 * se, m + 1.96 * se, int(len(v))


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 10:
        return float("nan")
    return float(pd.Series(a[ok]).rank().corr(pd.Series(b[ok]).rank()))


def fmt(x: float, nd: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "n/a"
    return f"{x:.{nd}f}".replace("-", "−")


# ───────────────────────────── stage: study ──────────────────────────────
class Study:
    def __init__(self) -> None:
        self.d = Data()
        self.lines: list[str] = []
        self.num: dict[str, Any] = {}
        d = self.d
        turns = {s: d.frame(d.m[s]["turn"]) for s in STOCKS}
        self.full_s = {s: ind.full_days(turns[s]) for s in STOCKS}
        nfull = sum(self.full_s[s].astype(int) for s in STOCKS)
        self.full = (nfull >= 15).to_numpy()  # basket full: 15 of 18 stocks full
        self.exp_s: dict[str, np.ndarray] = {}
        self.t_s: dict[str, np.ndarray] = {}
        tb = np.zeros((len(d.days), N))
        eb = np.zeros((len(d.days), N))
        for s in STOCKS:
            t = turns[s].to_numpy().copy()
            t[self.full_s[s].to_numpy()] = np.nan_to_num(t[self.full_s[s].to_numpy()])
            e = ind.expected_matrix(turns[s], self.full_s[s]).to_numpy()
            self.exp_s[s] = e
            self.t_s[s] = np.nan_to_num(t)
            tb += np.nan_to_num(t)
            eb += e
        self.tb, self.eb = tb, eb
        full_series = pd.Series(self.full, index=d.days)
        mu, sd = ind.zstats_matrix(d.frame(tb), full_series)
        self.zb = ind.zscore(ind.log_window(tb), mu.to_numpy(), sd.to_numpy())
        self.zb[~self.full] = np.nan
        self.mu_b, self.sd_b = mu.to_numpy(), sd.to_numpy()
        self.z_s = {}
        for s in STOCKS:
            mu_s, sd_s = ind.zstats_matrix(turns[s], self.full_s[s])
            z = ind.zscore(ind.log_window(self.t_s[s]), mu_s.to_numpy(), sd_s.to_numpy())
            z[~self.full] = np.nan
            self.z_s[s] = z
        self.rv5 = ind.safe_ratio(ind.window_sum(tb), ind.window_sum(eb))
        self.close = {k: ffill_rows(d.m[k]["close"]) for k in INDICES + [VIX]}
        self.use = self.full & np.isfinite(np.nanmax(np.where(np.isfinite(self.zb), 1, np.nan), 1))
        self.use &= d.period != ""

    def out(self, s: str = "") -> None:
        print(s, flush=True)
        self.lines.append(s)

    def pmask(self, name: str) -> np.ndarray:
        return self.use & (self.d.period == name)

    # V1 ─────────────────────────────────────────────────────────────────
    def v1(self) -> None:
        d = self.d
        self.out("== V1 the day's shape (basket turnover, full sessions) ==")
        rv = ind.day_rvol(d.frame(self.tb), d.frame(self.eb))
        expiry = ind.nifty_expiry_days(d.days[self.full])
        is_exp = np.array([day in expiry for day in d.days])
        v1: dict[str, Any] = {}
        for p in NAMES:
            m = self.pmask(p)
            prof = ind.profile(d.frame(self.tb).loc[m])
            hh = " ".join(f"{h['from']}:{h['share'] * 100:.1f}" for h in prof["half_hours"])
            self.out(f"{p} sessions {prof['sessions']}  {hh}")
            self.out(
                f"  busiest {prof['busiest']}  quietest {prof['quietest']} "
                f"quiet hour {prof['quiet_hour']}"
            )
            wd = ind.weekday_rvol(rv[m])
            self.out("  weekday rvol " + " ".join(f"{w['day']} {w['rvol']:.3f}" for w in wd))
            e = float(np.nanmean(rv[m & is_exp]))
            o = float(np.nanmean(rv[m & ~is_exp]))
            self.out(f"  NIFTY expiry days rvol {e:.3f} ({(m & is_exp).sum()}), others {o:.3f}")
            v1[p] = {"first_share": prof["first_share"], "busiest": prof["busiest"],
                     "quietest": prof["quietest"], "quiet_hour": prof["quiet_hour"],
                     "weekday": wd, "expiry_rvol": e, "other_rvol": o}  # fmt: skip
        self.num["v1"] = v1

    # V2 ─────────────────────────────────────────────────────────────────
    def v2(self) -> str:
        d = self.d
        self.out("\n== V2 which up-share rule (5-minute windows, basket flow share) ==")
        cols_all = np.arange(9, N, 5)
        res: dict[str, dict[str, dict[str, float]]] = {}
        for rule in ind.RULES:
            buy = sum(np.nan_to_num(d.m[s][f"buy_{rule}"]) for s in STOCKS)
            b5 = ind.window_sum(buy)
            t5 = ind.window_sum(self.tb)
            fs = ind.safe_ratio(b5, t5)
            res[rule] = {}
            for p in NAMES:
                rows = np.where(self.pmask(p))[0]
                rr = np.repeat(rows, len(cols_all))
                cc = np.tile(cols_all, len(rows))
                x = fs[rr, cc] - 0.5
                row: dict[str, float] = {}
                for k in ("NIFTY", "BANKNIFTY"):
                    c = self.close[k]
                    row[f"{k}_same"] = spearman(x, ret_back(c, cc, rr, 5))
                    row[f"{k}_next"] = spearman(x, ret_ahead(c, cc, rr, 5))
                res[rule][p] = row
            self.out(
                f"{rule:5s} "
                + "  ".join(
                    f"{p}: same {fmt(res[rule][p]['NIFTY_same'], 3)} next "
                    f"{fmt(res[rule][p]['NIFTY_next'], 3)} (BN {fmt(res[rule][p]['BANKNIFTY_same'], 3)}"
                    f"/{fmt(res[rule][p]['BANKNIFTY_next'], 3)})"
                    for p in NAMES
                )
            )
        best = max(ind.RULES, key=lambda r: res[r]["TUNE"]["NIFTY_same"])
        close = [
            r for r in ind.RULES
            if res[best]["TUNE"]["NIFTY_same"] - res[r]["TUNE"]["NIFTY_same"] <= 0.01
        ]  # fmt: skip
        chosen = max(close, key=lambda r: res[r]["TUNE"]["NIFTY_next"])
        self.out(f"chosen on TUNE: {chosen}")
        self.num["v2"] = {"by_rule": res, "chosen": chosen}
        self.rule = chosen
        buy = sum(np.nan_to_num(d.m[s][f"buy_{chosen}"]) for s in STOCKS)
        self.buy_b = buy
        self.buy_s = {s: np.nan_to_num(d.m[s][f"buy_{chosen}"]) for s in STOCKS}
        return chosen

    # V3 ─────────────────────────────────────────────────────────────────
    def alerts(self, zm: float, zs: float, cap: int = CAP, stocks: bool = True) -> list[tuple]:
        """(row, col, scope, key, z) of every alert on the usable sessions."""
        out = []
        for i in np.where(self.use)[0]:
            cands = [
                ind.Candidate(int(j), "MARKET", "MARKET", float(self.zb[i, j]))
                for j in np.where(self.zb[i] >= min(zm, 99))[0]
            ]
            if stocks:
                for s in STOCKS:
                    z = self.z_s[s][i]
                    cands += [
                        ind.Candidate(int(j), "STOCK", s, float(z[j])) for j in np.where(z >= zs)[0]
                    ]
            for c in ind.pick_alerts(
                cands, z_market=zm, z_stock=zs, cooldown_min=COOLDOWN, cap=cap
            ):
                out.append((i, c.t, c.scope, c.key, c.z))
        return out

    def v3(self) -> tuple[float, float]:
        self.out("\n== V3 alert levels (15-minute cooldown, no daily cap) ==")
        for p in NAMES:
            z = self.zb[self.pmask(p)]
            z = z[np.isfinite(z)]
            q = np.percentile(z, [50, 90, 99, 99.9])
            self.out(f"{p} market z: median {fmt(q[0])} p90 {fmt(q[1])} p99 {fmt(q[2])} "
                     f"p99.9 {fmt(q[3])} sd {fmt(z.std())}")  # fmt: skip
        per_day: dict[str, dict[str, list[float]]] = {"market": {}, "stock": {}}
        for lvl in MARKET_LEVELS:
            al = self.alerts(lvl, 99.0, cap=10**6, stocks=False)
            rows = np.array([a[0] for a in al], dtype=int)
            per_day["market"][str(lvl)] = [
                float((self.d.period[rows] == p).sum() / max(self.pmask(p).sum(), 1)) for p in NAMES
            ]
        for lvl in STOCK_LEVELS:
            al = self.alerts(99.0, lvl, cap=10**6, stocks=True)
            rows = np.array([a[0] for a in al if a[2] == "STOCK"], dtype=int)
            per_day["stock"][str(lvl)] = [
                float((self.d.period[rows] == p).sum() / max(self.pmask(p).sum(), 1)) for p in NAMES
            ]
        for scope, levels in per_day.items():
            for lvl, v in levels.items():
                self.out(f"{scope} z {lvl}: alerts a day " + " / ".join(fmt(x) for x in v))
        ok = [lv for lv in MARKET_LEVELS if 1.0 <= per_day["market"][str(lv)][0] <= 3.0]
        pool = ok or list(MARKET_LEVELS)
        zm = min(pool, key=lambda lv: abs(per_day["market"][str(lv)][0] - 2.0))
        oks = [lv for lv in STOCK_LEVELS if per_day["stock"][str(lv)][0] <= 6.0]
        zs = min(oks) if oks else max(STOCK_LEVELS)
        self.out(f"chosen on TUNE: market z {zm}, stock z {zs}")
        self.num["v3"] = {"per_day": per_day, "z_alert": zm, "stock_z_alert": zs}
        # the state words (set after V1 showed rvol runs above 1 on a normal day): quiet and
        # busy are the TUNE 15th and 85th percentiles of the basket's rvol_5m
        for p in NAMES:
            r = self.rv5[self.pmask(p)]
            r = r[np.isfinite(r)]
            q = np.percentile(r, [15, 50, 85])
            self.out(f"{p} rvol_5m p15 {fmt(q[0])} median {fmt(q[1])} p85 {fmt(q[2])}")
            self.num["v3"][f"rvol5_{p}"] = [float(x) for x in q]
        tq = self.num["v3"]["rvol5_TUNE"]
        self.num["v3"]["quiet"], self.num["v3"]["busy"] = round(tq[0], 1), round(tq[2], 1)
        self.zm, self.zs = zm, zs
        # the product's own walk at the chosen levels (cap 12)
        self.al = self.alerts(zm, zs)
        mk = [a for a in self.al if a[2] == "MARKET"]
        st = [a for a in self.al if a[2] == "STOCK"]
        for name, lst in (("market", mk), ("stock", st)):
            rows = np.array([a[0] for a in lst], dtype=int)
            v = [
                float((self.d.period[rows] == p).sum() / max(self.pmask(p).sum(), 1)) for p in NAMES
            ]
            self.out(f"with the cap of {CAP}: {name} alerts a day " + " / ".join(fmt(x) for x in v))
            self.num["v3"][f"{name}_per_day_capped"] = v
        return zm, zs

    # helpers for V4 to V8 ───────────────────────────────────────────────
    def market_alerts(self) -> tuple[np.ndarray, np.ndarray, list[tuple]]:
        mk = [a for a in self.al if a[2] == "MARKET"]
        return np.array([a[0] for a in mk], int), np.array([a[1] for a in mk], int), mk

    def near_mask(self, rows: np.ndarray, cols: np.ndarray, before: int, after: int) -> np.ndarray:
        """Session x minute: True where an alert lies within [m - before, m + after]."""
        a = np.zeros((len(self.d.days), N), bool)
        for r, c in zip(rows, cols, strict=True):
            a[r, max(0, c - after) : min(N, c + before + 1)] = True
        return a

    def baseline(
        self, values: np.ndarray, near: np.ndarray, p: str, how: str = "median"
    ) -> np.ndarray:
        """Per minute: the statistic of `values` (session x minute) over the period's usable
        sessions where no alert is near."""
        v = np.where(near | ~self.pmask(p)[:, None], np.nan, values)
        with np.errstate(all="ignore"):
            return np.nanmedian(v, 0) if how == "median" else np.nanmean(v, 0)

    def flow_of(self, row: int, col: int) -> float | None:
        b5 = ind.window_sum(self.buy_b[row : row + 1])[0, col]
        t5 = ind.window_sum(self.tb[row : row + 1])[0, col]
        return b5 / t5 if t5 > 0 else None

    def leaders(self, row: int, col: int, n: int = 2) -> list[str]:
        ex = {}
        for s in STOCKS:
            a = ind.window_sum(self.t_s[s][row : row + 1])[0, col]
            e = ind.window_sum(self.exp_s[s][row : row + 1])[0, col]
            ex[s] = a - e
        return sorted(ex, key=lambda k: -ex[k])[:n]

    # V4 ─────────────────────────────────────────────────────────────────
    def v4(self) -> None:
        self.out("\n== V4 size after a market alert: |index move| / median at that minute ==")
        rows, cols, _ = self.market_alerts()
        self.out(f"market alerts: {len(rows)}")
        res: dict[str, Any] = {}
        for k in INDICES:
            c = self.close[k]
            res[k] = {}
            for hz in (5, 15, 30, 60):
                absr = np.full_like(c, np.nan)
                absr[:, : N - hz] = np.abs(c[:, hz:] / c[:, : N - hz] - 1.0)
                near = self.near_mask(rows, cols, COOLDOWN, hz)
                cells = []
                for p in NAMES:
                    # both ratios are 1 when an alert changes nothing: the mean move over the
                    # mean at that minute, the median move over the median at that minute
                    base = self.baseline(absr, near, p)
                    bmean = self.baseline(absr, near, p, "mean")
                    sel = self.d.period[rows] == p
                    r, cc = rows[sel], cols[sel]
                    ratio = ind.safe_ratio(absr[r, cc], bmean[cc])
                    m, lo, hi, n = ci_by_day(ratio, r)
                    rmed = ind.safe_ratio(absr[r, cc], base[cc])
                    med = float(np.nanmedian(rmed)) if np.isfinite(rmed).any() else float("nan")
                    cells.append({"mean": m, "lo": lo, "hi": hi, "median": med, "n": n})
                res[k][hz] = cells
                self.out(
                    f"{k:9s} {hz:2d}m "
                    + " / ".join(
                        f"{fmt(x['mean'])} [{fmt(x['lo'])}, {fmt(x['hi'])}] med {fmt(x['median'])} n {x['n']}"
                        for x in cells
                    )
                )
        self.num["v4"] = res

    # V5 ─────────────────────────────────────────────────────────────────
    def v5(self) -> None:
        self.out("\n== V5 direction after a market alert (flow BUYING up, SELLING down) ==")
        rows, cols, _ = self.market_alerts()
        flow = np.array([self.flow_of(r, c) or np.nan for r, c in zip(rows, cols, strict=True)])
        sign = np.where(flow >= 0.6, 1.0, np.where(flow <= 0.4, -1.0, np.nan))
        self.out(
            f"directional alerts: {np.isfinite(sign).sum()} of {len(rows)} "
            f"(BUYING {(sign == 1).sum()}, SELLING {(sign == -1).sum()})"
        )
        res: dict[str, Any] = {}
        for k in INDICES:
            c = self.close[k]
            res[k] = {}
            for hz in (5, 15, 30):
                r = ret_ahead(c, cols, rows, hz)
                sr = sign * r
                cells = []
                for p in NAMES + ("ALL",):
                    sel = (self.d.period[rows] == p) if p != "ALL" else np.ones(len(rows), bool)
                    sel &= np.isfinite(sr)
                    m, t, n, nd = clustered(sr[sel] * 1e4, rows[sel])
                    hit = float((sr[sel] > 0).mean()) if sel.any() else float("nan")
                    cells.append({"hit": hit, "bps": m, "t": t, "n": n})
                res[k][hz] = cells
                self.out(
                    f"{k:9s} {hz:2d}m "
                    + " / ".join(
                        f"hit {fmt(x['hit'] * 100, 0)}% {fmt(x['bps'], 1)}bp t {fmt(x['t'], 1)} n {x['n']}"
                        for x in cells
                    )
                )
        self.out("after the 5 minutes BEFORE the alert (raw next move, bps, t):")
        split: dict[str, Any] = {}
        for k in ("NIFTY", "BANKNIFTY"):
            c = self.close[k]
            prior = ret_back(c, cols, rows, 5)
            split[k] = {}
            for hz in (5, 15, 30):
                r = ret_ahead(c, cols, rows, hz) * 1e4
                cells = {}
                for name, sel in (("fell", prior < 0), ("rose", prior > 0)):
                    per = []
                    for p in NAMES + ("ALL",):
                        ps = (self.d.period[rows] == p) if p != "ALL" else np.ones(len(rows), bool)
                        m, t, n, _ = clustered(r[sel & ps], rows[sel & ps])
                        per.append({"bps": m, "t": t, "n": n})
                    cells[name] = per
                split[k][hz] = cells
                self.out(
                    f"{k:9s} {hz:2d}m "
                    + "  ".join(
                        f"{nm}: "
                        + " / ".join(f"{fmt(x['bps'], 1)} ({fmt(x['t'], 1)}, {x['n']})" for x in v)
                        for nm, v in cells.items()
                    )
                )
        self.num["v5"] = {"follow": res, "after_prior": split}

    # V6 / V7 ────────────────────────────────────────────────────────────
    def weights(self) -> None:
        """Per session and index: NNLS influence weights and r2 from the previous 60 full
        sessions' one-minute returns (the product's `indicator.influence`)."""
        path = OUT / f"weights_{self.rule}.npz"
        if path.exists():
            z = np.load(path)
            self.w = {k: z[f"w_{k}"] for k in INDICES}
            self.r2 = {k: z[f"r2_{k}"] for k in INDICES}
            return
        d = self.d
        sc = {s: ffill_rows(d.m[s]["close"]) for s in STOCKS}
        sret = {s: sc[s][:, 1:] / sc[s][:, :-1] - 1.0 for s in STOCKS}
        iret = {k: self.close[k][:, 1:] / self.close[k][:, :-1] - 1.0 for k in INDICES}
        full_rows = np.where(self.full)[0]
        self.w = {k: np.full((len(d.days), len(STOCKS)), np.nan) for k in INDICES}
        self.r2 = {k: np.full(len(d.days), np.nan) for k in INDICES}
        t0 = time.time()
        for i in np.where(self.use)[0]:
            prev = full_rows[full_rows < i][-60:]
            if len(prev) < 20:
                continue
            x = pd.DataFrame({s: sret[s][prev].ravel() for s in STOCKS})
            for k in INDICES:
                y = pd.Series(iret[k][prev].ravel())
                w, r2 = ind.influence(y, x)
                self.w[k][i] = w.to_numpy()
                self.r2[k][i] = np.nan if r2 is None else r2
        print(f"weights: {time.time() - t0:.0f}s", flush=True)
        np.savez_compressed(
            path,
            **{f"w_{k}": self.w[k] for k in INDICES},
            **{f"r2_{k}": self.r2[k] for k in INDICES},
        )

    def v6(self) -> None:
        self.out("\n== V6 pressure_5m and the index's next minutes ==")
        self.weights()
        ratio = {}
        for s in STOCKS:
            net = 2 * self.buy_s[s] - self.t_s[s]
            ratio[s] = np.nan_to_num(
                ind.safe_ratio(ind.window_sum(net), ind.window_sum(self.exp_s[s]))
            )
        cols = np.arange(9, N - 5, 5)
        res: dict[str, Any] = {}
        for k in INDICES:
            pr = sum(self.w[k][:, [j]] * ratio[s] for j, s in enumerate(STOCKS))
            c = self.close[k]
            res[k] = {}
            for hz in (5, 15):
                cells = []
                for p in NAMES:
                    rows = np.where(self.pmask(p) & np.isfinite(self.w[k][:, 0]))[0]
                    rr = np.repeat(rows, len(cols))
                    cc = np.tile(cols, len(rows))
                    x = pr[rr, cc]
                    y = ret_ahead(c, cc, rr, hz) * 1e4
                    per_day = [spearman(x[rr == i], y[rr == i]) for i in rows]
                    pdv = np.array([v for v in per_day if np.isfinite(v)])
                    rho = float(pdv.mean()) if len(pdv) else float("nan")
                    t = (
                        float(pdv.mean() / pdv.std(ddof=1) * np.sqrt(len(pdv)))
                        if len(pdv) > 2
                        else float("nan")
                    )
                    ok = np.isfinite(x) & np.isfinite(y)
                    q20, q80 = np.percentile(x[ok], [20, 80])
                    sign = np.where(x >= q80, 1.0, np.where(x <= q20, -1.0, np.nan))
                    top = np.where(sign == 1, y, np.nan)
                    bot = np.where(sign == -1, y, np.nan)
                    spread = float(np.nanmean(top) - np.nanmean(bot))
                    # one session as one draw: the day's top minus bottom mean
                    dtop = pd.Series(top).groupby(rr).mean()
                    dbot = pd.Series(bot).groupby(rr).mean()
                    dd = (dtop - dbot).dropna()
                    ts = (
                        float(dd.mean() / dd.std(ddof=1) * np.sqrt(len(dd)))
                        if len(dd) > 2
                        else float("nan")
                    )
                    cells.append({"rho": rho, "t_rho": t, "spread_bps": spread, "t_spread": ts,
                                  "day_spread_bps": float(dd.mean()) if len(dd) else float("nan"),
                                  "sessions": int(len(pdv))})  # fmt: skip
                res[k][hz] = cells
                self.out(
                    f"{k:9s} {hz:2d}m "
                    + " / ".join(
                        f"rho {fmt(x['rho'], 3)} (t {fmt(x['t_rho'], 1)}) top-bottom pooled {fmt(x['spread_bps'], 1)}bp, by day {fmt(x['day_spread_bps'], 1)}bp (t {fmt(x['t_spread'], 1)})"
                        for x in cells
                    )
                )
        self.num["v6"] = res

    def v7(self) -> None:
        self.out("\n== V7 where it shows: influence weights and r2 ==")
        res: dict[str, Any] = {}
        for k in INDICES:
            r2 = [float(np.nanmean(self.r2[k][self.pmask(p)])) for p in NAMES]
            tm = self.pmask("TEST") & np.isfinite(self.w[k][:, 0])
            w = np.nanmean(self.w[k][tm], 0)
            top = sorted(zip(STOCKS, w, strict=True), key=lambda x: -x[1])[:5]
            self.out(f"{k:9s} r2 " + " / ".join(fmt(x, 2) for x in r2)
                     + "  TEST top " + ", ".join(f"{s} {v:.2f}" for s, v in top))  # fmt: skip
            res[k] = {"r2": r2, "top_test": [[s, round(float(v), 3)] for s, v in top]}
        rows, cols, _ = self.market_alerts()
        lead = [self.leaders(r, c) for r, c in zip(rows, cols, strict=True)]
        bank = np.array([all(x in BANKS for x in ll) for ll in lead])
        other = np.array([not any(x in BANKS for x in ll) for ll in lead])
        self.out(f"bank-led alerts {bank.sum()}, led by no bank {other.sum()} (of {len(rows)})")
        hz = 15
        near = self.near_mask(rows, cols, COOLDOWN, hz)
        grp: dict[str, Any] = {}
        for k in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            c = self.close[k]
            absr = np.full_like(c, np.nan)
            absr[:, : N - hz] = np.abs(c[:, hz:] / c[:, : N - hz] - 1.0)
            ratio = np.full(len(rows), np.nan)
            for p in NAMES:
                base = self.baseline(absr, near, p, "mean")
                sel = self.d.period[rows] == p
                ratio[sel] = ind.safe_ratio(absr[rows[sel], cols[sel]], base[cols[sel]])
            b = ci_by_day(ratio[bank], rows[bank])
            o = ci_by_day(ratio[other], rows[other])
            grp[k] = {"bank": b, "other": o}
            self.out(f"{k:9s} 15m size ratio: bank-led {fmt(b[0])} [{fmt(b[1])}, {fmt(b[2])}] n {b[3]}"
                     f"  others {fmt(o[0])} [{fmt(o[1])}, {fmt(o[2])}] n {o[3]}")  # fmt: skip
        self.num["v7"] = {"by_index": res, "bank_led": grp, "n_bank": int(bank.sum()),
                          "n_other": int(other.sum())}  # fmt: skip

    # V8 ─────────────────────────────────────────────────────────────────
    def v8(self) -> None:
        self.out("\n== V8 India VIX ==")
        rows, cols, mk = self.market_alerts()
        v = self.close[VIX]
        during = np.full_like(v, np.nan)
        during[:, 5:] = v[:, 5:] - v[:, :-5]
        after = np.full_like(v, np.nan)
        after[:, :-30] = v[:, 30:] - v[:, :-30]
        flow = np.array([self.flow_of(r, c) or np.nan for r, c in zip(rows, cols, strict=True)])
        res: dict[str, Any] = {}
        for name, mat, bef, aft in (("during", during, 5, 0), ("after30", after, COOLDOWN, 30)):
            near = self.near_mask(rows, cols, max(bef, COOLDOWN), aft)
            ex = np.full(len(rows), np.nan)
            for p in NAMES:
                base = self.baseline(mat, near, p, "mean")
                sel = self.d.period[rows] == p
                ex[sel] = mat[rows[sel], cols[sel]] - base[cols[sel]]
            cells = []
            for p in NAMES:
                for lab, fsel in (("all", np.ones(len(rows), bool)), ("SELLING", flow <= 0.4),
                                  ("BUYING", flow >= 0.6)):  # fmt: skip
                    sel = (self.d.period[rows] == p) & fsel
                    m, t, n, _ = clustered(ex[sel], rows[sel])
                    cells.append({"p": p, "flow": lab, "pts": m, "t": t, "n": n})
            res[name] = cells
            self.out(f"VIX change {name} the alert, over the same minutes' mean (points):")
            for p in NAMES:
                self.out("  " + p + " " + "  ".join(
                    f"{x['flow']} {fmt(x['pts'], 3)} (t {fmt(x['t'], 1)}, {x['n']})"
                    for x in cells if x["p"] == p))  # fmt: skip
        rv = ind.day_rvol(self.d.frame(self.tb), self.d.frame(self.eb)).to_numpy()
        vclose = np.array(
            [row[np.isfinite(row)][-1] if np.isfinite(row).any() else np.nan for row in v]
        )
        vchg = vclose - np.r_[np.nan, vclose[:-1]]
        prev = np.r_[np.nan, vclose[:-1]]
        tune = self.pmask("TUNE") & np.isfinite(prev)
        cuts = np.percentile(prev[tune], [100 / 3, 200 / 3])
        day: dict[str, Any] = {"cuts": [float(x) for x in cuts]}
        for p in NAMES:
            m = self.pmask(p)
            rho = spearman(rv[m], vchg[m])
            terc = [float(np.nanmean(rv[m & (np.digitize(prev, cuts) == i)])) for i in range(3)]
            day[p] = {"rho_rvol_vixchg": rho, "rvol_by_prev_vix": terc}
            self.out(f"{p} day rvol vs same-day VIX change rho {fmt(rho, 3)}; day rvol by the "
                     f"previous VIX close (low/mid/high, cuts {cuts[0]:.1f} {cuts[1]:.1f}): "
                     + " / ".join(fmt(x, 3) for x in terc))  # fmt: skip
        res["day"] = day
        self.num["v8"] = res

    # V9 ─────────────────────────────────────────────────────────────────
    def v9(self) -> None:
        self.out("\n== V9 the first half hour (09:15 to 09:44) and the rest of the day ==")
        a30 = self.tb[:, :30].sum(1)
        e30 = self.eb[:, :30].sum(1)
        rv30 = ind.safe_ratio(a30, e30)
        fs30 = ind.safe_ratio(self.buy_b[:, :30].sum(1), a30)
        res: dict[str, Any] = {}
        for k in ("NIFTY", "BANKNIFTY"):
            hi = ffill_rows(self.d.m[k]["high"])
            lo = ffill_rows(self.d.m[k]["low"])
            c = self.close[k]
            last = np.array(
                [row[np.isfinite(row)][-1] if np.isfinite(row).any() else np.nan for row in c]
            )
            rng = (np.nanmax(hi[:, 30:], 1) - np.nanmin(lo[:, 30:], 1)) / c[:, 29]
            s = pd.Series(np.where(self.full, rng, np.nan))
            med = s.shift(1).rolling(20, min_periods=10).median().to_numpy()
            rng_n = rng / med
            ret = (last / c[:, 29] - 1.0) * 1e4
            sign = np.sign(fs30 - 0.5)
            res[k] = {}
            for p in NAMES:
                m = self.pmask(p) & np.isfinite(rv30) & np.isfinite(rng_n)
                rho_size = spearman(rv30[m], rng_n[m])
                q = np.percentile(rv30[m], [20, 80])
                hi_r = float(np.nanmean(rng_n[m & (rv30 >= q[1])]))
                lo_r = float(np.nanmean(rng_n[m & (rv30 <= q[0])]))
                rho_dir = spearman(fs30[m], ret[m])
                sr = sign[m] * ret[m]
                hit = float((sr > 0).mean())
                mm, t, n, _ = clustered(sr, np.arange(m.sum()))
                res[k][p] = {"rho_size": rho_size, "range_top_fifth": hi_r, "range_bottom_fifth": lo_r,
                             "rho_dir": rho_dir, "hit": hit, "bps": mm, "t": t, "n": n}  # fmt: skip
                self.out(f"{k:9s} {p}: rvol30 vs rest range rho {fmt(rho_size, 3)} (range/median: "
                         f"top fifth {fmt(hi_r)}, bottom fifth {fmt(lo_r)}); flow30 vs rest return "
                         f"rho {fmt(rho_dir, 3)}, hit {fmt(hit * 100, 0)}%, {fmt(mm, 1)}bp t {fmt(t, 1)} n {n}")  # fmt: skip
        self.num["v9"] = res

    # the truncation check (RESEARCH-RULES 6) ────────────────────────────
    def truncation(self, n: int = 50) -> None:
        """Cut the bars after 50 random market alerts' minute and drop later sessions: the
        recomputed z and expected turnover at that minute must not change."""
        rows, cols, _ = self.market_alerts()
        rng = np.random.default_rng(7)
        pick = rng.choice(len(rows), size=min(n, len(rows)), replace=False)
        bad = 0
        for i in pick:
            r, c = int(rows[i]), int(cols[i])
            days = self.d.days[: r + 1]
            tb = self.tb[: r + 1].copy()
            tb[r, c + 1 :] = np.nan
            fr = pd.Series(self.full[: r + 1], index=days)
            mu, sd = ind.zstats_matrix(pd.DataFrame(tb, index=days, columns=range(N)), fr)
            row = ind.fill_through(tb[r], c)
            z = ind.zscore(ind.log_window(row), mu.to_numpy()[r], sd.to_numpy()[r])[0, c]
            e_cut = 0.0
            for s in STOCKS:
                ts = self.t_s[s][: r + 1].copy()
                ts[r, c + 1 :] = np.nan
                frame = pd.DataFrame(ts, index=days, columns=range(N))
                e_cut += ind.expected_matrix(frame, self.full_s[s].iloc[: r + 1]).to_numpy()[r, c]
            if not (np.isclose(z, self.zb[r, c]) and np.isclose(e_cut, self.eb[r, c])):
                bad += 1
        self.out(f"\ntruncation check: {len(pick)} alerts, {bad} changed")
        self.num["truncation"] = {"checked": int(len(pick)), "changed": bad}


def evidence(num: dict[str, Any]) -> dict[str, Any]:
    v4 = num["v4"]["NIFTY"]
    s15 = v4["15"] if "15" in v4 else v4[15]
    whole = [c["mean"] for c in s15]
    v5 = num["v5"]["follow"]["NIFTY"]
    d15 = (v5["15"] if "15" in v5 else v5[15])[3]
    v3 = num["v3"]
    facts = [
        {
            "id": "size_after_market_spike",
            "text": (
                f"After a market-wide spike NIFTY's next 15 minutes were {whole[0]:.1f}, "
                f"{whole[1]:.1f} and {whole[2]:.1f} times as large as usual (TUNE, CHOOSE, "
                "TEST): a size effect."
            ),
            "value": round(float(np.nanmean(whole)), 2),
        },
        {
            "id": "direction_after_market_spike",
            "text": (
                f"The next 15 minutes went the way of the flow {d15['hit'] * 100:.0f}% of the "
                f"time ({d15['bps']:+.1f} basis points, t {d15['t']:.1f}): no edge after an "
                "option buyer's costs."
            ).replace("-", "−"),
            "value": round(float(d15["hit"]), 2),
        },
    ]
    return {
        "as_of": "2026-11-24",
        "note": "docs/research/VOLUME_STUDY_2026-11-24.md",
        "facts": facts,
        "alerts_per_day": {
            "market": round(float(np.mean(v3["market_per_day_capped"])), 1),
            "stock": round(float(np.mean(v3["stock_per_day_capped"])), 1),
        },
    }


def stage_study() -> None:
    t0 = time.time()
    s = Study()
    print(f"loaded {len(s.d.days)} sessions, usable {s.use.sum()} in {time.time() - t0:.0f}s")
    s.out("sessions usable (basket full, z defined): "
          + " / ".join(str(int(s.pmask(p).sum())) for p in NAMES))  # fmt: skip
    s.v1()
    s.v2()
    s.v3()
    s.v4()
    s.v5()
    s.v6()
    s.v7()
    s.v8()
    s.v9()
    s.truncation()
    (OUT / "report.txt").write_text("\n".join(s.lines) + "\n")
    num = json.loads(json.dumps(s.num, default=float))
    (OUT / "numbers.json").write_text(json.dumps(num, indent=1))
    (OUT / "evidence.json").write_text(json.dumps(evidence(num), indent=1, ensure_ascii=False))
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for stage in sys.argv[1:] or ["load", "study"]:
        {"load": stage_load, "study": stage_study}[stage]()
