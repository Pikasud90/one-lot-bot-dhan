"""US-098: the long-volatility pair (S3 of US-092) on REAL prices of both legs, and the parity put.

Run from the MAIN checkout (the data is there), this file from the US-098 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/long_vol_real.py" <stage> [<stage> ...]

Stages
  trades     per index (NIFTY, BANKNIFTY), session and variant (entries 09:20 and 09:45; exits
             15:20, 12:00 and the -30% / +60% rule): the at-the-money CALL and PUT of the near
             monthly contract, both bought at Dhan's at-the-money strike of the entry minute and
             sold at THEIR OWN real prints of that strike at the exit (any rolling offset held).
             A trade without a real print of both legs at entry and exit is left out and
             counted. Also the call alone and the put alone, 09:20 to 15:20. Writes trades.csv,
             singles.csv and stop_needs.csv (the strikes the stop rule sold at that no store
             holds yet, for `real_puts_fetch.py plan --stops`).
  check      what came back: days asked and with bars, minutes a day, the strike against the
             index at 09:20, bad prints, and the parity put against the real put on every
             minute where both at-the-money legs are real (answer 2). check.txt
  select     the cells (index or pool x variant x day type) on the real reading, the selection
             rule of RESEARCH-RULES section 5, the placebo (20 shuffles of the day-type labels
             over the days inside each index and year), beside the earlier parity-put figure.
             cells.csv, placebo.csv
  report     the tables and the three answers' numbers -> report.txt

Read only: `data/expired_options.db` and this study's `options.sqlite` (SQLite `mode=ro`), the
harness's readers of `data/onelot.db`, and US-092's `days.csv` and `s3.csv` (day types, the
expiry calendar and the earlier parity readings). The US-092 study script is loaded by FILE
PATH from this worktree; the worktree's `src` is never on the path. No network.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import sqlite3
import sys
from pathlib import Path

WT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


OS = _load("option_setups_us098", WT / "scripts" / "option_setups.py")
h, rs = OS.h, OS.rs

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

from onelot.clock import IST  # noqa: E402

OUT = h.ROOT / "data" / "research" / "real_puts_2026-11-25"
MINE = OUT / "options.sqlite"
LEDGER = OUT / "ledger.sqlite"
APP = h.ROOT / "data" / "expired_options.db"
US092 = h.ROOT / "data" / "research" / "option_setups_2026-11-24"
INDICES = ["NIFTY_OPT", "BANKNIFTY_OPT"]
UND = {"NIFTY_OPT": "NIFTY", "BANKNIFTY_OPT": "BANKNIFTY"}
NAMES = OS.NAMES
DAY_TYPES = OS.DAY_TYPES
ENTRIES = ("09:20", "09:45")
EXITS = ("15:20", "12:00", "stop")
VARIANTS = [f"{a} {x}" for a in ENTRIES for x in EXITS]
MAX_WAIT = 3
Q_DIV = OS.Q_DIV
STOP_LO, STOP_HI = -0.30, 0.60
BAD_LO, BAD_HI = 0.25, 4.0  # the study's bad-print filter: volatility against the model's
SPY = h.SECONDS_PER_YEAR
SHUFFLES = 20


def mins(hhmm: str) -> int:
    return OS.mins(hhmm)


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def unpack(blob: bytes) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(blob)) as z:
        out = {"ts": z["ts"].astype("int64")}
        for c in ("open", "close", "strike", "spot"):
            out[c] = np.round(z[c].astype(float), 2)
        return out


# ───────────────────────────── real bars of one day ─────────────────────────


class Legs:
    """Every real one-minute bar of the near monthly contract (code 1) of one index, day and
    side, over every rolling offset held in our file and the app's store."""

    def __init__(self) -> None:
        self.dbs = [ro(p) for p in (MINE, APP) if p.is_file()]

    def day(self, und: str, day: dt.date, opt: str) -> dict[str, np.ndarray]:
        parts = []
        for db in self.dbs:
            q = ("SELECT rel, data FROM blocks WHERE underlying=? AND flag='MONTH' AND code=1"
                 " AND opt=? AND day=? AND rows > 0")  # fmt: skip
            for rel, blob in db.execute(q, (und, opt, day.isoformat())):
                z = unpack(blob)
                z["rel"] = np.full(len(z["ts"]), int(rel))
                parts.append(z)
        if not parts:
            return {k: np.zeros(0) for k in ("ts", "minute", "open", "close", "strike", "spot",
                                             "rel")}  # fmt: skip
        z = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
        z["minute"] = ((z["ts"] + 19800) % 86400) // 60
        # one bar per minute and strike (both stores hold the same prints where they overlap)
        key = z["minute"] * 1_000_000 + z["strike"].round().astype(np.int64)
        _, first = np.unique(key, return_index=True)
        return {k: v[np.sort(first)] for k, v in z.items()}

    def days_with(self, und: str, opt: str, rel: int = 0) -> dict[dt.date, int]:
        out: dict[dt.date, int] = {}
        for db in self.dbs:
            q = ("SELECT day, rows FROM blocks WHERE underlying=? AND flag='MONTH' AND code=1"
                 " AND opt=? AND rel=? AND rows > 0")  # fmt: skip
            for d, n in db.execute(q, (und, opt, rel)):
                dd = dt.date.fromisoformat(d)
                out[dd] = max(out.get(dd, 0), int(n))
        return out


def at_k(leg: dict, k: float, minute: int, field: str = "open") -> tuple[float, int]:
    """The real print of strike k at the first minute within 3 of `minute` (price, minute)."""
    sel = np.flatnonzero((leg["strike"] == k) & (leg["minute"] >= minute)
                         & (leg["minute"] <= minute + MAX_WAIT))  # fmt: skip
    if not len(sel):
        return np.nan, -1
    i = sel[np.argmin(leg["minute"][sel])]
    return float(leg[field][i]), int(leg["minute"][i])


def atm_at(leg: dict, minute: int) -> tuple[float, int]:
    """Dhan's at-the-money strike (rel 0) at the first minute within 3 of `minute`."""
    sel = np.flatnonzero((leg["rel"] == 0) & (leg["minute"] >= minute)
                         & (leg["minute"] <= minute + MAX_WAIT))  # fmt: skip
    if not len(sel):
        return np.nan, -1
    i = sel[np.argmin(leg["minute"][sel])]
    return float(leg["strike"][i]), int(leg["minute"][i])


def put_iv(p, f, k, t, r):  # noqa: ANN001, ANN201
    """Black-76 put volatility (through the call of the same strike by parity)."""
    t = np.maximum(np.asarray(t, float), 60.0 / SPY)
    disc = np.exp(-r * t)
    return OS.implied_vol(np.asarray(p, float) + disc * (np.asarray(f, float) - k), f, k, t, r)


def leg_iv(px, call: bool, spot, k, t, r):  # noqa: ANN001, ANN201
    f = np.asarray(spot, float) * np.exp((r - Q_DIV) * np.asarray(t, float))
    return OS.implied_vol(px, f, k, t, r) if call else put_iv(px, f, k, t, r)


# ───────────────────────────── stage: trades ────────────────────────────────


class Day:
    """One session of one index: both sides' bars, the expiry, the model's volatility."""

    def __init__(self, legs: Legs, und: str, d: dt.date, expiry: dt.date, iv_model: float,
                 r: float, step: float) -> None:  # fmt: skip
        self.und, self.d, self.r, self.step, self.ivm = und, d, r, step, iv_model
        self.ce = legs.day(und, d, "CE")
        self.pe = legs.day(und, d, "PE")
        exp = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=IST).timestamp()
        self.exp_s = exp
        self.day0 = dt.datetime.combine(d, dt.time(0, 0), tzinfo=IST).timestamp()

    def t_of(self, minute) -> np.ndarray:  # noqa: ANN001
        return np.maximum((self.exp_s - (self.day0 + np.asarray(minute) * 60.0)) / SPY,
                          60.0 / SPY)  # fmt: skip

    def leg(self, call: bool) -> dict:
        return self.ce if call else self.pe

    def spot_at(self, minute: int) -> float:
        for leg in (self.ce, self.pe):
            sel = np.flatnonzero(leg["minute"] == minute)
            if len(sel):
                return float(leg["spot"][sel[0]])
        return np.nan

    def bad(self, px: float, call: bool, k: float, minute: int) -> bool:
        """The study's filter: a print whose volatility is outside 0.25 to 4 times the model's
        is a bad print. Where the volatility says nothing (a price at or under its intrinsic
        value, or the last day of the contract, when an option is little more than its
        intrinsic value), the print is bad only when it is more than 0.2% of the index under
        its intrinsic value or carries a time value over 2% of the index."""
        s = self.spot_at(minute)
        if not np.isfinite(s) or not np.isfinite(self.ivm):
            return False
        t = self.t_of([minute])
        intrinsic = max(s - k, 0.0) if call else max(k - s, 0.0)
        iv = float(leg_iv(np.array([px]), call, np.array([s]), k, t, self.r)[0])
        floor_iv = np.isfinite(iv) and iv <= 0.0051
        if float(t[0]) * 365.0 < 1.0 or not np.isfinite(iv) or floor_iv:
            return px < intrinsic - 0.002 * s or px > intrinsic + 0.02 * s
        return iv < BAD_LO * self.ivm or iv > BAD_HI * self.ivm

    def estimate_closes(self, call: bool, k: float, minutes: np.ndarray) -> tuple:
        """Closes of strike k at `minutes`: the real close where a bar of k exists, else
        Black-76 at that minute's real at-the-money volatility of the same side (flat smile).
        Returns (values, real mask)."""
        leg = self.leg(call)
        out = np.full(len(minutes), np.nan)
        real = np.zeros(len(minutes), dtype=bool)
        pos_k = {int(m): i for i, m in enumerate(leg["minute"]) if leg["strike"][i] == k}
        atm = {int(leg["minute"][i]): i for i in np.flatnonzero(leg["rel"] == 0)}
        need, need_i = [], []
        for n, m in enumerate(minutes):
            i = pos_k.get(int(m))
            if i is not None:
                out[n], real[n] = leg["close"][i], True
            elif int(m) in atm:
                need.append(atm[int(m)])
                need_i.append(n)
        if need:
            ia = np.array(need)
            mm = minutes[np.array(need_i)]
            t = self.t_of(mm)
            ka = leg["strike"][ia]
            iv = leg_iv(leg["close"][ia], call, leg["spot"][ia], ka, t, self.r)
            f = leg["spot"][ia] * np.exp((self.r - Q_DIV) * t)
            out[np.array(need_i)] = OS.b76(f, k, t, iv, self.r, call)
        s = pd.Series(out)
        return s.ffill().to_numpy(), real


def rel_needed(day: Day, k: float, minute: int) -> float:
    """The rolling offset (in strike steps) that holds strike k at `minute`; NaN without an
    at-the-money bar. Dhan serves offsets -10 to +10 only."""
    a, _ = atm_at(day.ce, minute)
    if not np.isfinite(a):
        a, _ = atm_at(day.pe, minute)
    return (k - a) / day.step if np.isfinite(a) else np.nan


def fetchable(r: float) -> bool:
    return bool(np.isfinite(r) and abs(r - round(r)) < 1e-6 and abs(r) <= 10)


def pair_trade(p: h.Prep, day: Day, e_min: int, x_min: int, stop: bool) -> dict:
    """The pair bought at the entry minute's at-the-money strike, both legs at their real
    opens, sold at their real opens of the exit minute (or of the minute after a close at
    -30% / +60% of the pair). Every miss is named."""
    k, _ = atm_at(day.ce, e_min)
    if not np.isfinite(k):
        k, _ = atm_at(day.pe, e_min)
    out: dict = {"k": k, "why": "", "miss": "", "real_share": np.nan}
    if not np.isfinite(k):
        out["miss"] = "no at-the-money bar at entry"
        return out
    e = [at_k(day.ce, k, e_min), at_k(day.pe, k, e_min)]
    if not np.isfinite(e[0][0]) or not np.isfinite(e[1][0]):
        out["miss"] = "entry " + ("call" if not np.isfinite(e[0][0]) else "put")
        return out
    for call, (px, m) in zip((True, False), e, strict=True):
        if day.bad(px, call, k, m):
            out["miss"] = "bad print at entry"
            return out
    x = x_min
    out["why"] = "time"
    if stop:
        js = np.arange(max(e[0][1], e[1][1]), x_min)
        vc, rc = day.estimate_closes(True, k, js)
        vp, rp = day.estimate_closes(False, k, js)
        ratio = (vc + vp) / (e[0][0] + e[1][0]) - 1
        out["real_share"] = float(np.mean(rc & rp)) if len(js) else np.nan
        hit = np.flatnonzero(np.isfinite(ratio) & ((ratio <= STOP_LO) | (ratio >= STOP_HI)))
        if len(hit) and js[hit[0]] + 1 < x_min:
            x = int(js[hit[0]] + 1)
            out["why"] = "stop" if ratio[hit[0]] <= STOP_LO else "take"
    out["x_min"] = x
    xs = [at_k(day.ce, k, x), at_k(day.pe, k, x)]
    for call, (px, m) in zip((True, False), xs, strict=True):
        if not np.isfinite(px):
            out["miss"] = "exit " + ("call" if call else "put")
            r = rel_needed(day, k, x)
            if fetchable(r):
                out["need_rel"] = int(round(r))
            elif np.isfinite(r):
                # Dhan has no offset beyond ten strikes: the day is left out, and read at a
                # floor for the buyer (the leg in the money at its intrinsic value, if larger
                # than a print held; the other leg at its print or one tick)
                out["miss"] = "exit beyond ten strikes"
                sp = next((day.spot_at(x + w) for w in range(MAX_WAIT + 1)
                           if np.isfinite(day.spot_at(x + w))), np.nan)  # fmt: skip
                if np.isfinite(sp):
                    fl = [max(sp - k, 0.0), max(k - sp, 0.0)]
                    lb = [max(fl[i], xs[i][0] if np.isfinite(xs[i][0]) else 0.05)
                          for i in range(2)]  # fmt: skip
                    net, base = OS.leg_book(p, [e[0][0], e[1][0]], lb)
                    out["ret_lb"] = net / base * 100.0
            return out
        if day.bad(px, call, k, m):
            out["miss"] = "bad print at exit"
            return out
    net, base = OS.leg_book(p, [e[0][0], e[1][0]], [xs[0][0], xs[1][0]])
    out["ret"] = net / base * 100.0
    out["gross"] = (xs[0][0] + xs[1][0]) / (e[0][0] + e[1][0]) * 100 - 100
    out["e_call"], out["e_put"], out["x_call"], out["x_put"] = (e[0][0], e[1][0], xs[0][0],
                                                                 xs[1][0])  # fmt: skip
    return out


def single_trade(p: h.Prep, day: Day, call: bool, e_min: int, x_min: int) -> dict:
    """One at-the-money leg bought at its real open, sold at its real open of the exit."""
    k, _ = atm_at(day.ce, e_min)
    if not np.isfinite(k):
        k, _ = atm_at(day.pe, e_min)
    leg = day.leg(call)
    e, em = at_k(leg, k, e_min) if np.isfinite(k) else (np.nan, -1)
    x, xm = at_k(leg, k, x_min) if np.isfinite(k) else (np.nan, -1)
    if not (np.isfinite(e) and np.isfinite(x)) or day.bad(e, call, k, em) or day.bad(x, call,
                                                                                    k, xm):  # fmt: skip
        return {"ret": np.nan}
    net, base = OS.leg_book(p, [e], [x])
    return {"ret": net / base * 100.0}


def stage_trades() -> None:
    h._init_worker()
    days = OS.load_days()
    cfg = OS.bt_cfg()
    old = pd.read_csv(US092 / "s3.csv", parse_dates=["date"])
    old["date"] = old["date"].dt.date
    old = {(i, d, v): (m, r) for i, d, v, m, r in old[["inst", "date", "variant", "m", "r"]]
           .itertuples(index=False)}  # fmt: skip
    legs = Legs()
    rows, singles, needs = [], [], []
    for inst in INDICES:
        und = UND[inst]
        p = OS.prep(inst)
        step = float(h._cache.spec(inst).strike_step)
        dd = days[days.inst == inst].set_index("date")
        for s in OS.sessions(p):
            d = p.sess_dates[s]
            if d not in dd.index or pd.isna(dd.at[d, "expiry"]):
                continue
            base = OS.base_row(p, inst, s)
            day = Day(legs, und, d, dd.at[d, "expiry"], float(p.iv[s]),
                      cfg["r"], step)  # fmt: skip
            for at in ENTRIES:
                for xn in EXITS:
                    v = f"{at} {xn}"
                    x_min = mins("15:20") if xn in ("15:20", "stop") else mins("12:00")
                    t = pair_trade(p, day, mins(at), x_min, xn == "stop")
                    m_old, r_old = old.get((inst, d, v), (np.nan, np.nan))
                    rows.append({**base, "variant": v, "both": "ret" in t,
                                 "ret": t.get("ret", np.nan), "gross": t.get("gross", np.nan),
                                 "why": t["why"], "miss": t["miss"], "k": t["k"],
                                 "real_share": t["real_share"], "m_old": m_old, "r_old": r_old,
                                 "ret_lb": t.get("ret_lb", np.nan),
                                 **{c: t.get(c, np.nan) for c in ("e_call", "e_put", "x_call",
                                                                  "x_put")}})  # fmt: skip
                    if xn == "stop" and t.get("need_rel") not in (None, 0) and t["miss"]:
                        for opt in ("CE", "PE"):
                            needs.append({"underlying": und, "opt": opt,
                                          "rel": t["need_rel"], "day": d.isoformat()})  # fmt: skip
            sc = single_trade(p, day, True, mins("09:20"), mins("15:20"))
            sp = single_trade(p, day, False, mins("09:20"), mins("15:20"))
            singles.append({**base, "call": sc["ret"], "put": sp["ret"]})
        q = pd.DataFrame([r for r in rows if r["inst"] == inst])
        print(f"{inst}: {len(q)} rows, both legs real "
              f"{q.groupby('variant')['both'].mean().round(2).to_dict()}", flush=True)  # fmt: skip
    pd.DataFrame(rows).to_csv(OUT / "trades.csv", index=False)
    pd.DataFrame(singles).to_csv(OUT / "singles.csv", index=False)
    nd = pd.DataFrame(needs, columns=["underlying", "opt", "rel", "day"]).drop_duplicates()
    nd.to_csv(OUT / "stop_needs.csv", index=False)
    print(f"stop-rule exits that need a strike no store holds: {len(nd)}")


# ───────────────────────────── stage: check ─────────────────────────────────


def stage_check() -> None:
    h._init_worker()
    days = OS.load_days()
    cfg = OS.bt_cfg()
    legs = Legs()
    w = open(OUT / "check.txt", "w")  # noqa: SIM115

    def say(s: str = "") -> None:
        print(s, flush=True)
        w.write(s + "\n")

    if LEDGER.is_file():
        lg = ro(LEDGER)
        say("requests sent (ok, count): " + str(lg.execute(
            "SELECT ok, COUNT(*) FROM requests GROUP BY ok").fetchall()))  # fmt: skip
        say("requests by tier (tier, ok, count): " + str(lg.execute(
            "SELECT n.tier, r.ok, COUNT(*) FROM requests r LEFT JOIN needs n USING (underlying,"
            " code, opt, rel, day) GROUP BY 1, 2").fetchall()))  # fmt: skip
        say("first and last request: " + str(lg.execute(
            "SELECT MIN(at), MAX(at) FROM requests").fetchone()))  # fmt: skip
    pe_rows, par_rows = [], []
    for inst in INDICES:
        und = UND[inst]
        p = OS.prep(inst)
        step = float(h._cache.spec(inst).strike_step)
        dd = days[days.inst == inst].set_index("date")
        sess = [d for d in p.sess_dates if d in dd.index]
        pe = legs.days_with(und, "PE")
        ce = legs.days_with(und, "CE")
        for s, d in enumerate(p.sess_dates):
            if d not in dd.index:
                continue
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            j = OS.at_minute(p.minute, a, b, mins("09:20"))
            pe_rows.append({"inst": inst, "date": d, "year": d.year, "pe": pe.get(d, 0),
                            "ce": ce.get(d, 0), "idx920": p.o[j] if j >= 0 else np.nan,
                            "iv_model": float(p.iv[s])})  # fmt: skip
        q = pd.DataFrame([r for r in pe_rows if r["inst"] == inst])
        say(f"\n{und}: sessions {len(sess)}; with real at-the-money put bars "
            f"{(q.pe > 0).sum()}, call bars {(q.ce > 0).sum()}, both {((q.pe > 0) & (q.ce > 0)).sum()}")  # fmt: skip
        say("  by year, put / call days: " + ", ".join(
            f"{y} {int((g.pe > 0).sum())} / {int((g.ce > 0).sum())}"
            for y, g in q.groupby("year")))  # fmt: skip
        mp = q.pe[q.pe > 0]
        say(f"  put minutes a day: median {mp.median():.0f}, under 300 on "
            f"{int((mp < 300).sum())} days, full 375 on {int((mp >= 375).sum())}")  # fmt: skip
        # the strike against the index at 09:20, and bad prints, and parity
        at_money, near_idx, n920, bad, nbad = 0, 0, 0, 0, 0
        for r_ in q.itertuples():
            if r_.pe == 0:
                continue
            d = r_.date
            day = Day(legs, und, d, dd.at[d, "expiry"], r_.iv_model, cfg["r"], step)
            k, m = atm_at(day.pe, mins("09:20"))
            if np.isfinite(k) and np.isfinite(r_.idx920):
                n920 += 1
                i920 = np.flatnonzero((day.pe["rel"] == 0) & (day.pe["minute"] == m))[0]
                at_money += int(abs(k - day.pe["spot"][i920]) <= 0.5 * step + 1e-6)
                near_idx += int(abs(k - r_.idx920) <= step)
            pe0 = np.flatnonzero(day.pe["rel"] == 0)
            if len(pe0):
                t = day.t_of(day.pe["minute"][pe0])
                iv = leg_iv(day.pe["close"][pe0], False, day.pe["spot"][pe0],
                            day.pe["strike"][pe0], t, cfg["r"])  # fmt: skip
                nbad += len(pe0)
                bad += int(np.sum(~np.isfinite(iv) | (iv < BAD_LO * r_.iv_model)
                                  | (iv > BAD_HI * r_.iv_model)))  # fmt: skip
            ce0 = {int(day.ce["minute"][i]): i for i in np.flatnonzero(day.ce["rel"] == 0)}
            for i in pe0:
                mm = int(day.pe["minute"][i])
                c = ce0.get(mm)
                if c is None or day.ce["strike"][c] != day.pe["strike"][i]:
                    continue
                t = float(day.t_of([mm])[0])
                kk = float(day.pe["strike"][i])
                sp = float(day.pe["spot"][i])
                disc = np.exp(-cfg["r"] * t)
                row = {"inst": inst, "date": d, "minute": mm, "real": float(day.pe["close"][i]),
                       "call": float(day.ce["close"][c]), "k": kk, "spot": sp, "t": t}  # fmt: skip
                for q_ in (0.0, Q_DIV, 0.02):
                    f = sp * np.exp((cfg["r"] - q_) * t)
                    row[f"par_{q_}"] = row["call"] - disc * (f - kk)
                par_rows.append(row)
        say(
            f"  09:20 put strike within half a step of Dhan's spot {at_money} of {n920}, "
            f"within one step of our index open {near_idx} of {n920}"
        )
        say(f"  bad prints (volatility outside {BAD_LO} to {BAD_HI} times the model's): "
            f"{bad} of {nbad} put minutes ({bad / max(nbad, 1):.2%})")  # fmt: skip
    pr = pd.DataFrame(par_rows)
    pr.to_csv(OUT / "parity_minutes.csv", index=False)
    say("\nParity put against the real put, every minute both at-the-money legs are real:")
    for name, g in [("both", pr), *list(pr.groupby("inst"))]:
        for q_ in (0.0, Q_DIV, 0.02):
            e = (g[f"par_{q_}"] / g["real"] - 1) * 100
            say(f"  {name:13s} q {q_:.3f}: {len(g)} minutes on {g.date.nunique()} days, "
                f"error median {e.median():+.1f}%, IQR {e.quantile(0.25):+.1f} to "
                f"{e.quantile(0.75):+.1f}%, mean |err| {e.abs().mean():.1f}%")  # fmt: skip
    for mm in (mins("09:20"), mins("15:20")):
        g = pr[pr.minute == mm]
        e = (g[f"par_{Q_DIV}"] / g["real"] - 1) * 100
        say(f"  at {mm // 60:02d}:{mm % 60:02d} (q {Q_DIV}): {len(g)} days, median {e.median():+.1f}%,"
            f" IQR {e.quantile(0.25):+.1f} to {e.quantile(0.75):+.1f}%")  # fmt: skip
    # the forward the two real prints imply (C - P = disc (F - K)) against the assumed one
    pr["disc"] = np.exp(-cfg["r"] * pr["t"])
    pr["basis_bp"] = ((pr["k"] + (pr["call"] - pr["real"]) / pr["disc"]) / pr["spot"] - 1) * 1e4
    pr["assumed_bp"] = (np.exp((cfg["r"] - Q_DIV) * pr["t"]) - 1) * 1e4
    for name, g in [("both", pr), *list(pr.groupby("inst"))]:
        say(f"  forward implied by the real pair, {name}: median {g.basis_bp.median():+.0f} bp "
            f"over the index (IQR {g.basis_bp.quantile(0.25):+.0f} to "
            f"{g.basis_bp.quantile(0.75):+.0f}), assumed {g.assumed_bp.median():+.0f} bp")  # fmt: skip
    w.close()


# ───────────────────────────── stage: select ────────────────────────────────


def load_trades() -> pd.DataFrame:
    t = pd.read_csv(OUT / "trades.csv", parse_dates=["date"])
    t["date"] = t["date"].dt.date
    days = OS.load_days()
    fc = ["f_" + c for c in DAY_TYPES]
    t = t.merge(days[["inst", "date", *fc]], on=["inst", "date"], how="left")
    for c in fc:
        t[c] = t[c].fillna(False).astype(bool)
    return t


def cell_rows(t: pd.DataFrame, flags: pd.DataFrame | None = None) -> list[dict]:
    """Every cell on the real reading (both legs real), judged by RESEARCH-RULES section 5."""
    out = []
    real = t[t["both"].astype(bool)]
    for v in VARIANTS:
        q = real[real["variant"] == v]
        for c in DAY_TYPES:
            f = q["f_" + c] if flags is None else flags.loc[q.index, "f_" + c]
            g = q[f.to_numpy(bool)]
            d = pd.DataFrame({"inst": g["inst"], "dayoff": g["dayoff"], "period": g["period"],
                              "m": g["ret"], "r": g["ret"], "real": g["ret"]})  # fmt: skip
            for inst in [*INDICES, "POOL"]:
                dq = d if inst == "POOL" else d[d["inst"] == inst]
                st = OS.stats_of(dq)
                row = {"variant": v, "cond": c, "inst": inst.replace("_OPT", ""), **st,
                       **OS.judge(st)}  # fmt: skip
                if flags is None:
                    gi = g if inst == "POOL" else g[g["inst"] == inst]
                    row["old_r_same"] = gi["r_old"].mean()
                    allq = t[(t["variant"] == v) & t["f_" + c]]
                    if inst != "POOL":
                        allq = allq[allq["inst"] == inst]
                    row["old_r_all"] = allq["r_old"].mean()
                    row["old_n_all"] = int(allq["r_old"].notna().sum())
                    row["sessions"] = len(allq)
                    se = st["mean"] / st["t"] if st["t"] and np.isfinite(st["t"]) else np.nan
                    row["lo95"], row["hi95"] = st["mean"] - 1.96 * se, st["mean"] + 1.96 * se
                out.append(row)
    return out


COUNT_COLS = ["examined", "discovered", "confirmed", "passed", "avoid"]


def stage_select() -> None:
    t = load_trades()
    cells = pd.DataFrame(cell_rows(t))
    cells.to_csv(OUT / "cells.csv", index=False)
    c = cells[COUNT_COLS].sum()
    print("cells", len(cells), dict(c.astype(int)))
    rng = np.random.default_rng(98)
    fc = ["f_" + x for x in DAY_TYPES]
    t["year"] = [d.year for d in t["date"]]
    dayflags = t[["inst", "date", "year", *fc]].drop_duplicates(["inst", "date"])
    runs = []
    for k in range(SHUFFLES):
        parts = []
        for _, g in dayflags.groupby(["inst", "year"]):
            perm = rng.permutation(len(g))
            sh = g[["inst", "date"]].copy()
            sh[fc] = g[fc].to_numpy()[perm]
            parts.append(sh)
        sh = pd.concat(parts)
        fl = t[["inst", "date"]].merge(sh, on=["inst", "date"], how="left")
        fl.index = t.index
        pc = pd.DataFrame(cell_rows(t, fl))
        runs.append({"run": k, **pc[COUNT_COLS].sum().astype(int).to_dict()})
    pl = pd.DataFrame(runs)
    pl.to_csv(OUT / "placebo.csv", index=False)
    print("placebo mean", pl[COUNT_COLS].mean().round(2).to_dict(), "max",
          pl[COUNT_COLS].max().to_dict())  # fmt: skip


# ───────────────────────────── stage: report ────────────────────────────────


def f2(v: float, d: int = 2) -> str:
    return "n/a" if not np.isfinite(v) else f"{v:+.{d}f}".replace("-", "−")


def stage_report() -> None:
    t = load_trades()
    cells = pd.read_csv(OUT / "cells.csv")
    pl = pd.read_csv(OUT / "placebo.csv")
    sg = pd.read_csv(OUT / "singles.csv")
    w = open(OUT / "report.txt", "w")  # noqa: SIM115

    def say(s: str = "") -> None:
        print(s)
        w.write(s + "\n")

    say("Left out (no real print of both legs at entry and exit), by variant and reason:")
    for v in VARIANTS:
        q = t[t.variant == v]
        miss = q[~q.both.astype(bool)]["miss"].value_counts().to_dict()
        say(f"  {v:12s} sessions {len(q)}, both real {int(q.both.sum())}, left out {miss}")
        b = q[q.both.astype(bool)]
        lb = q[q.ret_lb.notna()]
        if len(lb):
            x = np.r_[b.ret.to_numpy(float), lb.ret_lb.to_numpy(float)]
            dd = np.r_[b.dayoff.to_numpy(), lb.dayoff.to_numpy()]
            say(f"    with the {len(lb)} days beyond ten strikes at a floor (intrinsic value): "
                f"{f2(b.ret.mean())} becomes {f2(x.mean())} (t {f2(rs.day_t(x, dd), 1)}); "
                f"those days alone {f2(lb.ret_lb.mean())}")  # fmt: skip
    stop = t[t.variant.str.endswith("stop") & t.both.astype(bool)]
    say(f"  stop rule: minutes watched on real closes of both legs, mean "
        f"{stop.real_share.mean():.0%}; exits {stop.why.value_counts().to_dict()}")  # fmt: skip
    say("\nCells (real both legs): index variant cond | days, % a pair (t), win, TUNE/CHOOSE/TEST"
        " | 95% interval | earlier parity same days / all days (n) | flags")  # fmt: skip
    for r in cells.itertuples():
        fl = "".join(k[0].upper() for k in COUNT_COLS if getattr(r, k))
        say(f"  {r.inst:9s} {r.variant:12s} {r.cond:11s} | {int(r.days):4d} {f2(r.mean)} "
            f"({f2(r.t, 1)}) {r.win:3.0f}% {f2(r.TUNE_m)} / {f2(r.CHOOSE_m)} / {f2(r.TEST_m)} | "
            f"{f2(r.lo95)} to {f2(r.hi95)} | {f2(r.old_r_same)} / {f2(r.old_r_all)} "
            f"({int(r.old_n_all)}) | {fl}")  # fmt: skip
    c = cells[COUNT_COLS].sum()
    ex = int(c["examined"])
    say(f"\nCounts: {dict(c.astype(int))}; placebo mean {pl[COUNT_COLS].mean().round(2).to_dict()}"
        f" max {pl[COUNT_COLS].max().to_dict()}; arithmetic discovered {ex * 0.006:.2f}, "
        f"confirmed {ex * 0.006 * 0.16:.3f}, passed {ex * 0.006 * 0.16 * 0.5:.3f}")  # fmt: skip
    # answer 2: the earlier parity reading against the real one, same trades
    say("\nEarlier parity-put reading against the real reading on the same trades:")
    for v in VARIANTS:
        q = t[(t.variant == v) & t.both.astype(bool) & t.r_old.notna()]
        dlt = q.r_old - q.ret
        say(f"  {v:12s} n {len(q)}: real {f2(q.ret.mean())}, parity {f2(q.r_old.mean())}, "
            f"parity less real {f2(dlt.mean())} (t {f2(rs.day_t(dlt.to_numpy(), q.dayoff), 1)})")  # fmt: skip
    # answer 3: one leg alone
    say("\nOne leg alone, at the money, 09:20 to 15:20, real both ends, one lot:")
    for leg in ("call", "put"):
        for inst in [*INDICES, "POOL"]:
            q = sg if inst == "POOL" else sg[sg.inst == inst]
            q = q[q[leg].notna()]
            x = q[leg].to_numpy(float)
            tt = rs.day_t(x, q.dayoff)
            per = " / ".join(f2(q[q.period == k][leg].mean()) for k in range(3))
            se = x.mean() / tt if np.isfinite(tt) and tt else np.nan
            say(f"  {leg:4s} {inst:13s} n {len(q)}: {f2(x.mean())} (t {f2(tt, 1)}), "
                f"95% {f2(x.mean() - 1.96 * se)} to {f2(x.mean() + 1.96 * se)}, "
                f"win {np.mean(x > 0) * 100:.0f}%, {per}")  # fmt: skip
    w.close()


STAGES = {"trades": stage_trades, "check": stage_check, "select": stage_select,
          "report": stage_report}  # fmt: skip


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for s in sys.argv[1:]:
        STAGES[s]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
