"""US-074: does the stop/target/trail rule (`src/onelot/manage/ratchet.py`) help? Read-only study.

Run from the MAIN checkout (the data is there), this file from the US-074 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. .venv/bin/python -W ignore \
        "<worktree>/scripts/ratchet_study.py" <stage> [<stage> ...]

Stages
  options  Part A: micro-trend entries on five indices, the baseline exit and 24 ratchet cells,
           then the sensitivity of the chosen cell. First proves the numpy exit equals the
           module's bar-by-bar loop on 2,000 random trades.
  control  Part B: random entries (20 seeds), hold to 15:15 and four ratchet cells.
  stocks   Part C: the pullback delivery rule on 18 stocks, baseline and 12 ratchet cells.
  real     real premiums (NIFTY, BANKNIFTY) for the chosen cell and the baseline; only from
           15:40 IST to 08:50 IST, 2 requests a second, the store first.
  report   the tables, to OUT/report.txt.

The ratchet module is loaded by FILE PATH from the worktree; the worktree's `src` is never on
the path. Market data is read only; `data/onelot.db` is never written. Outputs go to
data/research/ratchet_2026-11-23/ in the main checkout.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path.cwd() / "src"), str(Path.cwd() / "scripts")]

import microtrend_backtest as mb  # noqa: E402
import microtrend_study as ms  # noqa: E402
import mtf_bruteforce as h  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

WT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "ratchet_us074", WT / "src" / "onelot" / "manage" / "ratchet.py"
)
R = importlib.util.module_from_spec(_spec)
sys.modules["ratchet_us074"] = R  # dataclasses look the module up by name
_spec.loader.exec_module(R)

OUT = h.ROOT / "data" / "research" / "ratchet_2026-11-23"
EPS = R._EPS
NAMES = ("TUNE", "CHOOSE", "TEST")
OWNER4 = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
TRAINING = "MIDCPNIFTY_OPT"
COST_FRAC = 0.0005  # plan cost for an option: 0.05% of the index (stand-in, see the note)
REASONS = ("first stop", "breakeven stop", "lock stop", "trail stop", "target", "strategy",
           "session end")  # fmt: skip
LABEL_CODE = {"STOP": 0, "BREAKEVEN_STOP": 1, "LOCK_STOP": 2, "TRAIL_STOP": 3, "TARGET": 4}
STRATEGY, SESSION = 5, 6
REAL_FROM = dt.time(15, 40)
REAL_UNTIL = dt.time(8, 50)


# ───────────────────────────── the rule, vectorised ─────────────────────────


def code_of(reason: str) -> int:
    return LABEL_CODE[reason.removeprefix("GAP_")]


def ratchet_numpy(plan, prm, o, hi, lo):  # noqa: ANN001, ANN201
    """First exit of `plan` on bars (o, hi, lo): (bar, price, reason code, gap) or None.

    Same arithmetic as `exit_on_bar` then `advance`: the running best price gives the
    progress; the stop, target and stage it sets hold from the NEXT bar (shift by one)."""
    s, e, mv = plan.side, plan.entry, plan.move
    fav, adv = (hi, lo) if s == 1 else (lo, hi)
    best = np.maximum.accumulate(np.maximum(fav, e)) if s == 1 else (
        np.minimum.accumulate(np.minimum(fav, e)))  # fmt: skip
    prog = s * (best - e) / mv
    on_be = prog >= prm.be_at - EPS
    w = np.where(on_be, plan.cost + prm.be_lock * mv, -np.inf)
    w = np.where(prog >= prm.lock_at - EPS, np.maximum(w, prm.lock * mv), w)
    on_ex = prog >= prm.extend_at - EPS
    w = np.where(on_ex, np.maximum(w, (prog - prm.trail_gap) * mv), w)
    w = np.where(on_be, np.minimum(w, (prog - prm.min_gap) * mv), -np.inf)
    held = np.maximum(s * (plan.stop - e), np.maximum.accumulate(w))
    stop = e + s * held
    stage = np.maximum.accumulate(
        np.where(on_ex, 3, np.where(prog >= prm.lock_at - EPS, 2, np.where(on_be, 1, 0)))
    )
    if prm.extend:
        reach = np.ones(len(prog))
        near = 1.0 - prm.extend_at
        m = prog >= reach - near - EPS
        while m.any():
            reach[m] += prm.extend_by
            m = prog >= reach - near - EPS
        target = np.where(reach == 1.0, plan.target, e + s * reach * mv)
    else:
        target = np.full(len(prog), plan.target)
    # the levels in force on bar k are the ones set after bar k-1
    S = np.r_[plan.stop, stop[:-1]]  # noqa: N806
    T = np.r_[plan.target, target[:-1]]  # noqa: N806
    G = np.r_[0, stage[:-1]]  # noqa: N806
    gs = s * (o - S) <= 0
    st = s * (adv - S) <= 0
    gt = s * (o - T) >= 0
    tg = s * (fav - T) >= 0
    anyx = gs | st | gt | tg
    if not anyx.any():
        return None
    k = int(np.argmax(anyx))
    if gs[k]:
        return k, float(o[k]), int(G[k]), True
    if st[k]:
        return k, float(S[k]), int(G[k]), False
    if gt[k]:
        return k, float(o[k]), 4, True
    return k, float(T[k]), 4, False


def ratchet_loop(plan, prm, o, hi, lo):  # noqa: ANN001, ANN201
    """The module itself, bar by bar."""
    st = R.start(plan)
    for k in range(len(o)):
        hit = R.exit_on_bar(plan, st, float(o[k]), float(hi[k]), float(lo[k]))
        if hit:
            return k, float(hit[1]), code_of(hit[0]), hit[0].startswith("GAP_")
        st, _ = R.advance(plan, st, float(hi[k]), float(lo[k]), prm)
    return None


def verify(p: h.Prep, atr: dict, n: int = 2000, seed: int = 74) -> None:
    rng = np.random.default_rng(seed)
    cand = np.flatnonzero(
        p.inwin & (p.year >= 0) & np.isfinite(atr["15m"]) & np.isfinite(atr["60m"])
    )
    same = 0
    hits = 0
    for _ in range(n):
        j = int(rng.choice(cand))
        side = int(rng.choice([1, -1]))
        tf = str(rng.choice(["15m", "60m"]))
        k = float(rng.choice([0.5, 1.0, 1.5, 2.0]))
        prm = R.RatchetParams(extend=bool(rng.integers(2)),
                              trail_gap=float(rng.choice([0.15, 0.2, 0.3])))  # fmt: skip
        plan = R.plan_from_risk(side, float(p.o[j]), k * float(atr[tf][j]),
                                float(rng.choice([2.0, 3.0, 4.0])), COST_FRAC * float(p.o[j]))  # fmt: skip
        b = int(p.xi[p.sess[j]])
        a1 = ratchet_numpy(plan, prm, p.o[j:b], p.h[j:b], p.l[j:b])
        a2 = ratchet_loop(plan, prm, p.o[j:b], p.h[j:b], p.l[j:b])
        ok = (a1 is None and a2 is None) or (
            a1 is not None and a2 is not None and a1[0] == a2[0] and a1[2:] == a2[2:]
            and abs(a1[1] - a2[1]) <= 1e-6 * abs(a2[1])
        )  # fmt: skip
        assert ok, (p.inst, j, side, tf, k, prm, a1, a2)
        same += 1
        hits += a2 is not None
    print(f"verify {p.inst}: numpy equals the module on {same} of {n} random trades "
          f"({hits} exited by the rule)", flush=True)  # fmt: skip


# ───────────────────────────── Part A: options ─────────────────────────────


def atr_all(p: h.Prep) -> dict[str, np.ndarray]:
    """ATR(14, Wilder) of 15m and 60m on the last bar closed at each minute's open."""
    idx = np.arange(p.n)
    return {tf: ms.tf_age_atr(p, tf, idx)[1] for tf in ("15m", "60m")}


def next_against(p: h.Prep, need: int = 3) -> dict[int, np.ndarray]:
    """For each side, the first k >= i in steps of 3 where `need` of 7 point against."""
    out = {}
    for side in (1, -1):
        ev = (p.D == -side).sum(axis=0) >= need
        nx = np.full(p.n + 3, p.n, dtype=np.int64)
        for r in range(3):
            sub = ev[r::3]
            idx = np.where(sub, np.arange(r, p.n, 3), p.n)
            nx[r : p.n : 3] = np.minimum.accumulate(idx[::-1])[::-1]
        out[side] = nx
    return out


def cells() -> list[dict]:
    out = []
    for tf in ("15m", "60m"):
        for k in (1.0, 1.5, 2.0):
            for ext in (True, False):
                for keep in (True, False):
                    out.append({"tf": tf, "k": k, "extend": ext, "keep": keep, "target_r": 3.0,
                                "trail_gap": 0.20})  # fmt: skip
    return out


def cell_name(c: dict | None) -> str:
    if c is None:
        return "baseline"
    if c.get("hold"):
        return "hold to 15:15"
    s = f"{c['tf']} k{c['k']:.1f} {'ext' if c['extend'] else 'fix'} {'kept' if c['keep'] else 'drop'}"
    if c["target_r"] != 3.0 or c["trail_gap"] != 0.20:
        s += f" T{c['target_r']:g} g{c['trail_gap']:g}"
    return s


def one_exit(p, j, side, cell, atr, nxa):  # noqa: ANN001, ANN201
    """Exit of an entry at bar j: (bar, price, reason, how). `cell` None: the reference exit."""
    s = int(p.sess[j])
    xi = int(p.xi[s])
    xs = int(nxa[side][j + 3]) if (cell is None or cell.get("keep")) and j + 3 < p.n else p.n
    if xs < xi:
        end, why, px, how = xs, STRATEGY, float(p.o[xs]), "open"
    else:
        end, why, px = xi, SESSION, float(p.xp[s])
        how = "close" if p.xclose[s] else "open"
    if cell is None or cell.get("hold"):
        return end, px, why, how
    stop_end = end + 1 if (why == SESSION and p.xclose[s]) else end
    risk = cell["k"] * float(atr[cell["tf"]][j])
    if not (risk > 0):
        raise ValueError(f"no ATR at bar {j} of {p.inst}")
    e = float(p.o[j])
    plan = R.plan_from_risk(side, e, risk, cell["target_r"], COST_FRAC * e)
    prm = R.RatchetParams(extend=cell["extend"], trail_gap=cell["trail_gap"])
    hit = ratchet_numpy(plan, prm, p.o[j:stop_end], p.h[j:stop_end], p.l[j:stop_end])
    if hit is None:
        return end, px, why, how
    k, lvl, code, gap = hit
    return j + k, lvl, code, ("open" if gap else "touch")


def micro_trades(p, cell, atr, nxa):  # noqa: ANN001, ANN201
    """`mb.trades_of` with the exit of `cell`: one trade at a time, so entries depend on it."""
    first = p.sess_first[p.sess]
    boundary = ((np.arange(p.n) - first) % 3 == 0) & (np.arange(p.n) > first)
    up = (p.D == 1).all(axis=0)
    dn = (p.D == -1).all(axis=0)
    rows = []
    prev_up = prev_dn = False
    busy = -1
    for j in np.flatnonzero(boundary):
        a_up, a_dn = bool(up[j]), bool(dn[j])
        if j > busy and p.inwin[j]:
            side = 1 if (a_up and not prev_up) else (-1 if (a_dn and not prev_dn) else 0)
            if side and j < p.xi[p.sess[j]]:
                x, px, why, how = one_exit(p, int(j), side, cell, atr, nxa)
                rows.append((int(j), x, side, px, why, how))
                busy = x
        prev_up, prev_dn = a_up, a_dn
    return rows


def priced(p: h.Prep, rows: list, label: str, extra: dict | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    E, X, S, PX, W, HOW = map(np.array, zip(*rows, strict=True))  # noqa: N806
    ret = np.empty(len(E))
    strike = np.empty(len(E))
    for side in (1, -1):
        m = side == S
        if m.any():
            ret[m] = h.pnl(p, side, 0, E[m], X[m], PX[m], p.ns[X[m]])["ret"]
            strike[m] = p.opt(side).strike[E[m]]
    keep = p.year[E] >= 0
    df = pd.DataFrame({
        "cell": label, "inst": p.inst, "day": p.dayoff[E], "period": mb.PERIOD[p.year[E]],
        "side": S, "ret": ret, "bp": S * (PX / p.o[E] - 1) * 1e4, "why": W, "held": X - E,
        "e0": p.ns[E] // 1_000_000_000, "x0": p.ns[X] // 1_000_000_000, "strike": strike,
        "how": HOW, "level": PX,
    })[keep]  # fmt: skip
    for k, v in (extra or {}).items():
        df[k] = v
    return df


def stage_options() -> None:
    h._init_worker()
    grid = cells()
    frames = []
    preps = {}
    for inst in h.INDEX_OPTIONS:
        t0 = time.time()
        p = h.prepare(inst, tfs=mb.TFS)
        atr = atr_all(p)
        nxa = next_against(p)
        if inst == h.INDEX_OPTIONS[0]:
            verify(p, atr)
        ref = mb.trades_of(p)
        mine = micro_trades(p, None, atr, nxa)
        assert [r[0] for r in mine] == ref["e"].tolist(), "baseline entries differ"
        assert [r[1] for r in mine] == ref["x"].tolist(), "baseline exits differ"
        frames.append(priced(p, mine, "baseline"))
        for c in grid:
            frames.append(priced(p, micro_trades(p, c, atr, nxa), cell_name(c)))
        print(f"{inst}: {time.time() - t0:.0f}s", flush=True)
        preps[inst] = (p, atr, nxa)
    df = pd.concat(frames, ignore_index=True)
    df.to_csv(OUT / "options.csv", index=False)
    b = df[df.cell == "baseline"]
    print(f"baseline, five indices: {len(b)} trades, {b.ret.mean():+.2f}% a trade, "
          f"{100 * (b.ret > 0).mean():.1f}% winners", flush=True)  # fmt: skip
    best = choose(df)
    c0 = next(c for c in grid if cell_name(c) == best)
    print(f"chosen cell: {best}", flush=True)
    sens = []
    for var in ({"target_r": 2.0}, {"target_r": 4.0}, {"trail_gap": 0.15}, {"trail_gap": 0.30}):
        c = {**c0, **var}
        for p, atr, nxa in preps.values():
            sens.append(priced(p, micro_trades(p, c, atr, nxa), cell_name(c)))
    pd.concat(sens, ignore_index=True).to_csv(OUT / "sensitivity.csv", index=False)
    (OUT / "chosen.json").write_text(json.dumps(c0), encoding="utf-8")


def choose(df: pd.DataFrame) -> str:
    """Fixed in advance: highest TUNE mean among cells whose CHOOSE mean is not below the
    baseline's (four owner indices)."""
    d = df[df.inst.isin(OWNER4)]
    per = d.groupby(["cell", "period"]).ret.mean().unstack()
    base_c = per.loc["baseline", 1]
    ok = per[(per.index != "baseline") & (per[1] >= base_c)]
    if ok.empty:
        return "baseline"
    return str(ok[0].idxmax())


# ───────────────────────────── Part B: control ─────────────────────────────


def stage_control() -> None:
    h._init_worker()
    opt = pd.read_csv(OUT / "options.csv")
    c0 = json.loads((OUT / "chosen.json").read_text())
    grid = {cell_name(c): c for c in cells()}
    others = ["15m k1.0 ext drop", "60m k1.5 fix kept", "60m k2.0 ext drop", "15m k2.0 fix kept"]
    exits = [{"hold": True}, c0] + [grid[n] for n in others if n != cell_name(c0)][:3]
    base = opt[opt.cell == "baseline"]
    rows = []
    for inst in h.INDEX_OPTIONS:
        t0 = time.time()
        p = h.prepare(inst, tfs=mb.TFS)
        atr = atr_all(p)
        nxa = next_against(p)
        cnt = base[base.inst == inst].groupby("day").size()
        first = p.sess_first[p.sess]
        bnd = ((np.arange(p.n) - first) % 3 == 0) & (np.arange(p.n) > first) & p.inwin
        bnd &= np.arange(p.n) < p.xi[p.sess]
        bnd &= np.isfinite(atr["15m"]) & np.isfinite(atr["60m"])
        day_of_sess = p.dayoff[p.sess_first]
        avail = {}
        for j in np.flatnonzero(bnd):
            avail.setdefault(int(p.sess[j]), []).append(int(j))
        sess_need = [(s, int(cnt.get(int(day_of_sess[s]), 0))) for s in sorted(avail)]
        for seed in range(20):
            for c in exits:
                rng = np.random.default_rng(1000 * seed + h.INDEX_OPTIONS.index(inst))
                tr = []
                for s, need in sess_need:
                    a = np.array(avail[s])
                    busy = -1
                    for _ in range(need):
                        cand = a[a > busy]
                        if not len(cand):
                            break
                        j = int(rng.choice(cand))
                        side = int(rng.choice([1, -1]))
                        x, px, why, how = one_exit(p, j, side, c, atr, nxa)
                        tr.append((j, x, side, px, why, how))
                        busy = x
                rows.append(priced(p, tr, cell_name(c), {"seed": seed}))
        print(f"{inst}: {time.time() - t0:.0f}s", flush=True)
    pd.concat(rows, ignore_index=True).drop(columns=["e0", "x0", "strike", "how", "level"]).to_csv(
        OUT / "control.csv", index=False
    )


# ───────────────────────────── Part C: stocks ──────────────────────────────

SLIP = h.STOCK_SLIPPAGE
DEPTH, RULE_STOP, MAX_SESS = 0.08, 0.15, 30
CUT1, CUT2 = dt.date(2024, 11, 8), dt.date(2025, 11, 8)


def wilder_atr(hi: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([hi - lo, np.abs(hi - pc), np.abs(lo - pc)]), axis=0)
    out = np.full(len(c), np.nan)
    acc = tr[:n].mean()
    out[n - 1] = acc
    for i in range(n, len(c)):
        acc = (acc * (n - 1) + tr[i]) / n
        out[i] = acc
    return out


def stock_cells() -> list[dict]:
    return [{"k": k, "extend": ext, "keep": keep} for k in (1.5, 2.0, 3.0) for ext in (True, False)
            for keep in (True, False)]  # fmt: skip


def stock_name(c: dict | None) -> str:
    if c is None:
        return "baseline"
    return (
        f"daily k{c['k']:.1f} {'ext' if c['extend'] else 'fix'} {'kept' if c['keep'] else 'drop'}"
    )


class Stock:
    def __init__(self, inst: str) -> None:
        key = h._cache.spec(inst).series_key
        b = h._cache.bars(key, "1d")
        self.key = key
        self.d = np.array([x.date() for x in b.index])
        self.o, self.hi, self.lo, self.c = (
            b[k].to_numpy(float) for k in ("open", "high", "low", "close")
        )
        self.s200, self.s20 = sma(self.c, 200), sma(self.c, 20)
        self.atr = wilder_atr(self.hi, self.lo, self.c)
        m = h._cache.bars(key, "1m")
        md = np.array(m.index.date)
        self.mo, self.mh, self.ml = (m[k].to_numpy(float) for k in ("open", "high", "low"))
        pos = {x: i for i, x in enumerate(self.d)}
        di = np.array([pos.get(x, -1) for x in md])
        self.m_first = np.full(len(self.d), -1, dtype=np.int64)
        self.m_last = np.full(len(self.d), -1, dtype=np.int64)
        chg = np.flatnonzero(np.r_[True, di[1:] != di[:-1]])
        ends = np.r_[chg[1:], len(di)]
        for a, z in zip(chg, ends, strict=True):
            if di[a] >= 0:
                self.m_first[di[a]], self.m_last[di[a]] = a, z
        self.fallback = 0
        # the reference's market filter: NIFTY's close above its SMA200 on the signal day
        nb = h._cache.bars(h._cache.spec("NIFTY_OPT").series_key, "1d")
        nc = nb["close"].to_numpy(float)
        ok = dict(zip([x.date() for x in nb.index], (nc > sma(nc, 200)).tolist(), strict=True))
        self.mkt = np.array([ok.get(x, False) for x in self.d])

    def bars(self, e: int, x: int) -> tuple[np.ndarray, ...]:
        """Minute bars of days e..x-1 (a day with no minute bars: its daily bar), and the day of
        each bar."""
        parts, days = [], []
        for u in range(e, x):
            a, z = self.m_first[u], self.m_last[u]
            if a >= 0:
                parts.append((self.mo[a:z], self.mh[a:z], self.ml[a:z]))
                days.append(np.full(z - a, u))
            else:
                self.fallback += 1
                parts.append((self.o[u : u + 1], self.hi[u : u + 1], self.lo[u : u + 1]))
                days.append(np.array([u]))
        o, hi, lo = (np.concatenate([q[i] for q in parts]) for i in range(3))
        return o, hi, lo, np.concatenate(days)


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1 :] = (cs[n:] - cs[:-n]) / n
    return out


def stock_trades(st: Stock, cell: dict | None, cost) -> list[tuple]:  # noqa: ANN001
    """The reference R4 loop (alpha_pullback_check.trades: depth 8%, NIFTY above SMA200, closing stop 15%) with the
    ratchet laid over it. Rows: (entry day, exit day, buy, sell price before slippage, why,
    exit is a whole day later)."""
    o, c, s200, s20 = st.o, st.c, st.s200, st.s20
    n = len(c)
    out, t = [], 200
    while t < n - 1:
        hi20 = c[t - 19 : t + 1].max()
        hi_prev = c[t - 20 : t].max()
        first = c[t] <= (1 - DEPTH) * hi20 and not c[t - 1] <= (1 - DEPTH) * hi_prev
        if not (first and c[t] > s200[t] and st.mkt[t]):
            t += 1
            continue
        e = t + 1
        buy = o[e] * (1 + SLIP)
        # the rule's own exit (kept): first close above SMA20, 30 sessions, close 15% under
        xr, at_close = None, False
        for u in range(e, n):
            if c[u] > s20[u] or u - e + 1 >= MAX_SESS or c[u] <= buy * (1 - RULE_STOP):
                xr, at_close = (u + 1, False) if u + 1 < n else (u, True)
                break
        if xr is None:
            xr, at_close = n - 1, True
        x, px, why = xr, (c[xr] if at_close else o[xr]), STRATEGY
        if cell is not None:
            if not cell["keep"]:
                x, px, why, at_close = n - 1, c[n - 1], SESSION, True
            risk = cell["k"] * st.atr[t]
            rt = (cost.leg(np.array([buy]), False)[0] + cost.leg(np.array([buy]), True)[0]
                  + 2 * SLIP * buy + h.DP_CHARGE_INR / max(1, round(1e4 / buy)))  # fmt: skip
            plan = R.plan_from_risk(1, buy, risk, 3.0, rt)
            prm = R.RatchetParams(extend=cell["extend"])
            last = x + 1 if at_close else x
            hit, span = None, 60
            while True:
                z = min(last, e + span)
                bo, bh, bl, bd = st.bars(e, z)
                hit = ratchet_numpy(plan, prm, bo, bh, bl)
                if hit is not None or z == last:
                    break
                span *= 4
            if hit is not None:
                k, lvl, code, _gap = hit
                x, px, why = int(bd[k]), lvl, code
        out.append((t, e, x, px, why))
        t = x  # no new signal is read before the bar the trade ends on
    return out


def stage_stocks() -> None:
    h._init_worker()
    cost = h.LegCost.from_config(h._cost_cfg.product("equity_delivery"))
    rows = []
    for inst in h.STOCKS:
        t0 = time.time()
        st = Stock(inst)
        win = (st.d >= h.T0) & (st.d <= h.T_END)
        ow = st.o[win]

        def bench(days: int, ow: np.ndarray = ow, bench_cache: dict = {}) -> float:  # noqa: B006
            if days not in bench_cache:
                if days > 0 and len(ow) > days:
                    g = ow[days:] / ow[:-days]
                    bs = g * (1 - SLIP)
                    bch = cost.leg(np.array([1.0]), sell=False)[0] + cost.leg(bs, sell=True)
                    bench_cache[days] = float(((bs - (1 + SLIP) - bch) / (1 + SLIP) * 100).mean())
                else:
                    bench_cache[days] = 0.0
            return bench_cache[days]

        for c in [None, *stock_cells()]:
            for _t, e, x, px, why in stock_trades(st, c, cost):
                if not (h.T0 <= st.d[e] <= h.T_END):
                    continue
                buy = st.o[e] * (1 + SLIP)
                sell = px * (1 - SLIP)
                ch = cost.leg(np.array([buy]), False)[0] + cost.leg(np.array([sell]), True)[0]
                net = (sell - buy - ch) / buy * 100
                q = max(1, round(1e4 / buy))
                dp = h.DP_CHARGE_INR if x > e else 0.0  # a same-day sale leaves no demat debit
                rows.append({
                    "cell": stock_name(c), "inst": inst, "day": st.d[e].toordinal(),
                    "period": 0 if st.d[e] < CUT1 else (1 if st.d[e] < CUT2 else 2),
                    "net": net, "ret": net - dp / (q * buy) * 100, "ret1": net - dp / buy * 100,
                    "bp": (px / st.o[e] - 1) * 1e4, "why": why, "held": x - e,
                    "bench": bench(x - e),
                })  # fmt: skip
        print(f"{inst}: {time.time() - t0:.0f}s, days on daily bars {st.fallback}", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "stocks.csv", index=False)
    b = df[df.cell == "baseline"]
    print(f"baseline: {len(b)} trades, {b.net.mean():+.2f}% a trade before DP, "
          f"{100 * (b.net > 0).mean():.0f}% winners", flush=True)  # fmt: skip


# ───────────────────────────── real premiums ───────────────────────────────


def stage_real() -> None:
    now = ms.now_ist().time()
    if REAL_UNTIL <= now < REAL_FROM:
        raise SystemExit(f"real: only from {REAL_FROM} to {REAL_UNTIL} IST (now {now:%H:%M})")
    import mtf_real_premiums as mr
    from expired_options_fetch import STORE_PATH, research_adapter, underlyings

    from onelot.broker.base import TokenExpiredError
    from onelot.config import CostConfig
    from onelot.costs import CostModel
    from onelot.data.expired_options import OptionBarStore
    from onelot.lab.real_options import NeedBlock

    h._init_worker()
    c0 = json.loads((OUT / "chosen.json").read_text())
    df = pd.read_csv(OUT / "options.csv")
    df = df[df.cell.isin(["baseline", cell_name(c0)]) & df.inst.isin(list(ms.REAL_SERIES))]
    real_f = OUT / "real.jsonl"
    done = set()
    if real_f.exists():
        done = {(r["inst"], r["e0"], r["x0"], r["how"], r["level"])
                for r in map(json.loads, real_f.read_text().splitlines())}  # fmt: skip
    logf = (OUT / "real.log").open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{ms.now_ist():%H:%M:%S} IST] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    state = {"stop": None}
    try:
        adapter = research_adapter(2.0)
    except Exception as exc:  # noqa: BLE001
        adapter, state["stop"] = None, f"adapter not built ({type(exc).__name__}); store only"
        log(state["stop"])

    class StudyPricer(mr.CarryPricer):
        def _block(self, key, day):  # type: ignore[no-untyped-def]
            t = ms.now_ist().time()
            if self.source is not None and REAL_UNTIL <= t < REAL_FROM:
                self.source = None
                state["stop"] = state["stop"] or "08:50 reached"
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
    for inst, series in ms.REAL_SERIES.items():
        spec = h._cache.spec(inst)
        bars = h._cache.bars(spec.series_key, "1m")
        bars = bars[bars.index < pd.Timestamp(h.T_END + dt.timedelta(days=1), tz=bars.index.tz)]
        costs = CostModel(CostConfig.load(
            h._settings.project_root / "config" / "costs.yaml").product(spec.product))  # fmt: skip
        pricers[inst] = StudyPricer(unds[series], spec, bt, costs, store, bars, 1,
                                    source=adapter, slip_pct=0.0)  # fmt: skip
    t0 = time.time()
    trades = df.drop_duplicates(["inst", "e0", "x0", "how", "level"]).sort_values(["inst", "e0"])
    with real_f.open("a", encoding="utf-8") as fh:
        for i, t in enumerate(trades.itertuples()):
            key = (t.inst, int(t.e0), int(t.x0), t.how, float(t.level))
            if key in done:
                continue
            try:
                real = pricers[t.inst].price_one(int(t.e0), int(t.x0), float(t.strike), int(t.side),
                                                 t.how, float(t.level), {"ret": float(t.ret), "inr": 0.0})  # fmt: skip
            except NeedBlock:
                real = {"unpriced": "block not fetched"}
            except Exception as exc:  # noqa: BLE001
                real = {"unpriced": f"error: {type(exc).__name__}: {str(exc)[:80]}"}
            fh.write(json.dumps({"inst": t.inst, "e0": key[1], "x0": key[2], "how": t.how,
                                 "level": key[4], "real_ret": real.get("real_ret"),
                                 "unpriced": real.get("unpriced")}, default=str) + "\n")  # fmt: skip
            fh.flush()
            if i % 200 == 0:
                req = sum(q.requests for q in pricers.values())
                log(
                    f"{i}/{len(trades)} requests {req} {time.time() - t0:.0f}s stop={state['stop']}"
                )
    log(f"END requests {sum(q.requests for q in pricers.values())} stop={state['stop']}")


# ───────────────────────────── report ──────────────────────────────────────


def day_t(x: np.ndarray, d: np.ndarray) -> float:
    """t of the mean with all trades of one entry day as one draw."""
    x = np.asarray(x, float)
    if len(x) < 3:
        return float("nan")
    _, inv = np.unique(np.asarray(d), return_inverse=True)
    dev = np.bincount(inv, x) - x.mean() * np.bincount(inv)
    se = math.sqrt(float((dev**2).sum())) / len(x)
    return float(x.mean() / se) if se > 0 else float("nan")


def summary(d: pd.DataFrame, col: str = "ret") -> dict:
    x = d[col].to_numpy(float)
    win, loss = x[x > 0], x[x <= 0]
    aw = win.mean() if len(win) else np.nan
    al = loss.mean() if len(loss) else np.nan
    per = [d.loc[d.period == k, col].mean() for k in range(3)]
    shares = np.bincount(d["why"].astype(int), minlength=7) / max(len(d), 1) * 100
    return {"n": len(x), "m": x.mean(), "t": day_t(x, d["day"].to_numpy()),
            "bp": d["bp"].mean(), "w": (x > 0).mean() * 100, "aw": aw, "al": al,
            "pay": aw / abs(al) if al else np.nan, "per": per, "why": shares}  # fmt: skip


HDR = (f"{'cell':<30}{'n':>6} {'%/trade':>7} {'t':>5} {'bp':>6} {'win%':>5} {'avgW':>6} {'avgL':>6}"
       f" {'pay':>5}  {'TUNE / CHOOSE / TEST':<22} exits %: 1st/BE/lock/trail/tgt/strat/sess")  # fmt: skip


def row(name: str, s: dict) -> str:
    per = " / ".join(f"{v:+.2f}" for v in s["per"])
    why = "/".join(f"{v:.0f}" for v in s["why"])
    return (f"{name:<30}{s['n']:>6} {s['m']:>+7.2f} {s['t']:>+5.1f} {s['bp']:>+6.1f} {s['w']:>5.1f}"
            f" {s['aw']:>+6.2f} {s['al']:>+6.2f} {s['pay']:>5.2f}  {per:<22} {why}")  # fmt: skip


def stage_report() -> None:
    L: list[str] = []  # noqa: N806
    w = L.append
    opt = pd.read_csv(OUT / "options.csv")
    five = opt[opt.cell == "baseline"]
    w(f"REPRODUCTION: baseline five indices {len(five)} trades, {five.ret.mean():+.2f}% a trade, "
      f"{100 * (five.ret > 0).mean():.1f}% winners (target 3,824, -0.24%, 38.3%)")  # fmt: skip
    chosen = choose(opt)
    w(f"CHOSEN (rule fixed in advance; 24 cells tried): {chosen}")
    w("")
    w("PART A: four owner indices, model premiums, sorted by TUNE")
    w(HDR)
    own = opt[opt.inst.isin(OWNER4)]
    stats = {c: summary(g) for c, g in own.groupby("cell")}
    for c in sorted(stats, key=lambda k: -stats[k]["per"][0]):
        w(row(c + (" *" if c == chosen else ""), stats[c]))
    w("")
    w("MIDCPNIFTY (training instrument, not in the totals)")
    mid = opt[opt.inst == TRAINING]
    for c in ["baseline", chosen]:
        w(row(c, summary(mid[mid.cell == c])))
    w("")
    w("PER INDEX, baseline and chosen")
    for inst in OWNER4:
        for c in ["baseline", chosen]:
            w(
                row(
                    f"{inst.replace('_OPT', '')} {c}",
                    summary(own[(own.inst == inst) & (own.cell == c)]),
                )
            )
    w("")
    w("CHOSEN minus BASELINE, per entry day (paired days only, four indices)")
    a = own[own.cell == chosen].groupby("day").ret.sum()
    b = own[own.cell == "baseline"].groupby("day").ret.sum()
    j = pd.concat([a, b], axis=1, keys=["c", "b"]).fillna(0.0)
    dd = (j.c - j.b).to_numpy()
    w(
        f"   days {len(dd)}, mean daily difference {dd.mean():+.3f} points (t {dd.mean() / (dd.std(ddof=1) / math.sqrt(len(dd))):+.2f})"
    )
    w("")
    sp = OUT / "sensitivity.csv"
    if sp.exists():
        sens = pd.read_csv(sp)
        w("SENSITIVITY of the chosen cell (four indices)")
        w(HDR)
        w(row(chosen, stats[chosen]))
        for c, g in sens[sens.inst.isin(OWNER4)].groupby("cell", sort=False):
            w(row(c, summary(g)))
        w("")
    rp = OUT / "real.jsonl"
    if rp.exists():
        rr = pd.DataFrame([json.loads(x) for x in rp.read_text().splitlines() if x.strip()])
        rr = rr.drop_duplicates(["inst", "e0", "x0", "how", "level"])
        w("REAL PREMIUMS, NIFTY + BANKNIFTY (like for like: real where priced, model less the "
          "measured gap where not)")  # fmt: skip
        for c in ["baseline", chosen]:
            d = opt[(opt.cell == c) & opt.inst.isin(list(ms.REAL_SERIES))].merge(
                rr, on=["inst", "e0", "x0", "how", "level"], how="left")  # fmt: skip
            d["real_ret"] = pd.to_numeric(d["real_ret"], errors="coerce")
            pr = d[d.real_ret.notna()]
            gap = float((pr.real_ret - pr.ret).mean()) if len(pr) else float("nan")
            d["est"] = d.real_ret.fillna(d.ret + gap)
            s_est = summary(d, "est")
            w(f"   {c}: trades {len(d)}, priced {len(pr)}, model {d.ret.mean():+.2f}%, "
              f"real (priced) {pr.real_ret.mean():+.2f}% vs model (same) {pr.ret.mean():+.2f}%, gap {gap:+.2f}")  # fmt: skip
            w(row(f"   {c} like-for-like", s_est))
        w("")
    cp = OUT / "control.csv"
    if cp.exists():
        ctl = pd.read_csv(cp)
        ctl = ctl[ctl.inst.isin(OWNER4)]
        w("PART B: random entries, 20 seeds, four indices: mean (sd, min to max) over seeds")
        for c, g in ctl.groupby("cell", sort=False):
            per_seed = [summary(gg) for _, gg in g.groupby("seed")]
            m = np.array([s["m"] for s in per_seed])
            wr = np.array([s["w"] for s in per_seed])
            pay = np.array([s["pay"] for s in per_seed])
            n = np.mean([s["n"] for s in per_seed])
            w(f"   {c:<24} trades {n:5.0f}  %/trade {m.mean():+.2f} (sd {m.std(ddof=1):.2f}, "
              f"{m.min():+.2f} to {m.max():+.2f})  win {wr.mean():.1f}% (sd {wr.std(ddof=1):.1f})"
              f"  payoff {pay.mean():.2f} (sd {pay.std(ddof=1):.2f})")  # fmt: skip
        w("")
    stp = OUT / "stocks.csv"
    if stp.exists():
        sk = pd.read_csv(stp)
        b = sk[sk.cell == "baseline"]
        w(f"PART C REPRODUCTION: {len(b)} trades, {b.net.mean():+.2f}% before DP, "
          f"{100 * (b.net > 0).mean():.0f}% winners (target 112, +1.36%, 75%)")  # fmt: skip
        per = sk.groupby(["cell", "period"]).ret.mean().unstack()
        ok = per[(per.index != "baseline") & (per[1] >= per.loc["baseline", 1])]
        pick = str(ok[0].idxmax()) if len(ok) else "baseline"
        w(f"PART C chosen by the same rule (12 cells tried): {pick}")
        w(
            "PART C: 18 stocks, % a trade after charges and DP at shares nearest Rs 10,000; sorted by TUNE"
        )
        w(HDR + "   | one share | excess over hold (t)")
        st = dict(list(sk.groupby("cell")))
        for c in sorted(st, key=lambda k: -summary(st[k])["per"][0]):
            g = st[c]
            s = summary(g)
            exc = (g.ret - g.bench).to_numpy()
            w(
                row(c + (" *" if c == pick else ""), s)
                + f"   | {g.ret1.mean():+.2f} | {exc.mean():+.2f} ({day_t(exc, g.day.to_numpy()):+.1f})"
            )
        w("   (exit code 5 = the rule's own exit; 6 = still open at the data end)")
    text = "\n".join(L) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stages = sys.argv[1:] or ["report"]
    for name in stages:
        {"options": stage_options, "control": stage_control, "stocks": stage_stocks,
         "real": stage_real, "report": stage_report}[name]()  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
