"""US-074 verifier: independent re-derivation of the ratchet study's numbers.

Run from the MAIN checkout (the data is there), this file from the US-074 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. .venv/bin/python -W ignore \
        "<worktree>/scripts/ratchet_study_check.py"

Checks (written without calling the study's exit functions; the study's own functions are
used only to name what it computed, never to produce a result being checked):
  A  headline figures of four option cells from options.csv against the note's table
  B  300 random trades of each of three ratchet cells re-simulated bar by bar with the
     module (own ATR, own strategy and session exits)
  C  look-ahead: own ATR from one-minute bars cut at the entry minute
  D  stocks: baseline and `k3.0 ext kept` from stocks.csv, 40 trades re-simulated
  E  the control: no seed positive, mean of "hold to 15:15"
Writes data/research/ratchet_2026-11-23/check_report.txt in the main checkout.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

sys.path[:0] = [str(Path.cwd() / "src"), str(Path.cwd() / "scripts")]

import microtrend_study as ms  # noqa: E402
import mtf_bruteforce as h  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

WT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "ratchet_check", WT / "src" / "onelot" / "manage" / "ratchet.py"
)
R = importlib.util.module_from_spec(_spec)
sys.modules["ratchet_check"] = R
_spec.loader.exec_module(R)

OUT = h.ROOT / "data" / "research" / "ratchet_2026-11-23"
NOTE = WT / "docs" / "research" / "RATCHET_STUDY_2026-11-23.md"
OWNER4 = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
CELLS_A = ["baseline", "60m k2.0 ext kept", "60m k1.5 ext kept", "15m k1.0 ext kept"]
CELLS_B = CELLS_A[1:]
CODE = {"STOP": 0, "BREAKEVEN_STOP": 1, "LOCK_STOP": 2, "TRAIL_STOP": 3, "TARGET": 4}
LINES: list[str] = []


def say(msg: str = "") -> None:
    print(msg, flush=True)
    LINES.append(msg)


def norm(s: str) -> str:
    return s.replace("−", "-").strip()


# ───────────────────────────── own statistics ──────────────────────────────


def day_t(x: np.ndarray, day: np.ndarray) -> float:
    """t of the mean, all trades of one entry day one draw (cluster-robust)."""
    x = np.asarray(x, float)
    dev = pd.Series(x - x.mean()).groupby(np.asarray(day)).sum()
    se = math.sqrt(float((dev**2).sum())) / len(x)
    return float(x.mean() / se)


def figures(d: pd.DataFrame, col: str = "ret") -> dict:
    x = d[col].to_numpy(float)
    per = [float(d.loc[d.period == k, col].mean()) for k in range(3)]
    shares = np.bincount(d["why"].astype(int).to_numpy(), minlength=7) / len(d) * 100
    return {
        "n": len(d),
        "mean": float(x.mean()),
        "t": day_t(x, d["day"].to_numpy()),
        "win": float((x > 0).mean() * 100),
        "per": per,
        "exits": [float(v) for v in shares],
    }


def fmt_fig(f: dict) -> dict[str, str]:
    return {
        "n": str(f["n"]),
        "mean (t)": f"{f['mean']:+.2f} ({f['t']:+.1f})",
        "win": f"{f['win']:.1f}",
        "T/C/T": " / ".join(f"{v:+.2f}" for v in f["per"]),
        "exits": "/".join(f"{v:.0f}" for v in f["exits"]),
    }


def note_table(heading: str) -> dict[str, list[str]]:
    """Rows of the first markdown table after `heading` in the note: name -> cells."""
    lines = NOTE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(heading))
    rows: dict[str, list[str]] = {}
    seen = False
    for ln in lines[start + 1 :]:
        if ln.startswith("|"):
            seen = True
            cells = [norm(c) for c in ln.strip().strip("|").split("|")]
            if cells[0] in ("cell", "exit") or set(cells[0]) <= {"-"}:
                continue
            rows[re.sub(r" \(.*\)$", "", cells[0])] = cells
        elif seen:
            break
    return rows


# ───────────────────────────── own ATR ─────────────────────────────────────

_IST_NS = 19_800 * 1_000_000_000
_DAY_NS = 86_400 * 1_000_000_000
_MIN_NS = 60 * 1_000_000_000


def my_atr(ns: np.ndarray, hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, tf: int, n: int = 14):
    """ATR(14, Wilder, seeded by the mean of the first 14 true ranges) of `tf`-minute bars
    built from one-minute bars, bins anchored at 09:15 IST each session. Returns (nominal
    close ns of each bar, ATR at each bar)."""
    local = ns + _IST_NS
    day = local // _DAY_NS
    mod = (local % _DAY_NS) // _MIN_NS
    slot = (mod - 555) // tf  # 09:15 is minute 555
    key = day * 10_000 + (slot + 500)
    first = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    last = np.r_[first[1:], len(key)] - 1
    bh = np.maximum.reduceat(hi, first)
    bl = np.minimum.reduceat(lo, first)
    bc = cl[last]
    close_ns = (day[first] * _DAY_NS + (555 + (slot[first] + 1) * tf) * _MIN_NS) - _IST_NS
    pc = np.r_[np.nan, bc[:-1]]
    tr = np.maximum(bh - bl, np.maximum(np.abs(bh - pc), np.abs(bl - pc)))
    tr[0] = bh[0] - bl[0]
    atr = np.full(len(tr), np.nan)
    if len(tr) >= n:
        acc = tr[:n].mean()
        atr[n - 1] = acc
        for i in range(n, len(tr)):
            acc = (acc * (n - 1) + tr[i]) / n
            atr[i] = acc
    return close_ns, atr


def atr_at(close_ns: np.ndarray, atr: np.ndarray, t_ns: int) -> float:
    pos = int(np.searchsorted(close_ns, t_ns, side="right")) - 1
    return float(atr[pos]) if pos >= 0 else float("nan")


# ───────────────────────────── Part A ──────────────────────────────────────


def part_a(opt: pd.DataFrame) -> None:
    say("=" * 100)
    say("A. HEADLINE FIGURES FROM options.csv (four owner indices) AGAINST THE NOTE")
    say("=" * 100)
    table = note_table("| cell | trades")
    own = opt[opt.inst.isin(OWNER4)]
    bad = 0
    for c in CELLS_A:
        f = fmt_fig(figures(own[own.cell == c]))
        row = table[c]
        theirs = {
            "n": row[1],
            "mean (t)": row[2],
            "win": row[4],
            "T/C/T": row[8],
            "exits": row[9],
        }
        say(f"cell {c}")
        for k in f:
            ok = f[k] == theirs[k]
            bad += not ok
            say(f"   {k:<10} mine {f[k]:<28} note {theirs[k]:<28} {'ok' if ok else 'DIFFERENT'}")
    say(f"A: {'matches' if bad == 0 else f'{bad} figures differ'}")
    say()


# ───────────────────────────── Part B and C ────────────────────────────────


def parse_cell(name: str) -> tuple[int, float, bool]:
    m = re.fullmatch(r"(\d+)m k([\d.]+) (ext|fix) kept", name)
    return int(m.group(1)), float(m.group(2)), m.group(3) == "ext"


def my_session_exit(p: h.Prep, s: int) -> tuple[int, float, bool]:
    """Bar, price and 'is a close' of the 15:15 option exit of session s."""
    a, b = int(p.sess_first[s]), int(p.sess_last[s])
    late = np.flatnonzero(p.minute[a : b + 1] >= 15 * 60 + 15)
    if len(late) and p.minute[a] < 15 * 60 + 15:
        k = a + int(late[0])
        return k, float(p.o[k]), False
    return b, float(p.c[b]), True


def resim(p: h.Prep, j: int, side: int, risk: float, extend: bool):
    """Exit of an entry at minute bar j: (bar, price, reason code, how). Own strategy and
    session exits; the ratchet through the module's exit_on_bar then advance, bar by bar."""
    s = int(p.sess[j])
    xi, xpx, xclose = my_session_exit(p, s)
    end, code, px, how = xi, 6, xpx, ("close" if xclose else "open")
    k = j + 3
    while k < xi:
        if int((p.D[:, k] == -side).sum()) >= 3:
            end, code, px, how = k, 5, float(p.o[k]), "open"
            break
        k += 3
    last = end + 1 if (code == 6 and xclose) else end
    entry = float(p.o[j])
    plan = R.plan_from_risk(side, entry, risk, 3.0, 0.0005 * entry)
    prm = R.RatchetParams(extend=extend)
    st = R.start(plan)
    for i in range(j, last):
        hit = R.exit_on_bar(plan, st, float(p.o[i]), float(p.h[i]), float(p.l[i]))
        if hit:
            return (
                i,
                float(hit[1]),
                CODE[hit[0].removeprefix("GAP_")],
                ("open" if hit[0].startswith("GAP_") else "touch"),
            )
        st, _ = R.advance(plan, st, float(p.h[i]), float(p.l[i]), prm)
    return end, px, code, how


def part_bc(opt: pd.DataFrame) -> None:
    say("=" * 100)
    say("B. BAR-BY-BAR RE-SIMULATION, 300 RANDOM TRADES OF EACH OF THREE RATCHET CELLS")
    say("=" * 100)
    rng = np.random.default_rng(20261123)
    own = opt[opt.inst.isin(OWNER4)]
    preps: dict[str, h.Prep] = {}
    atrs: dict[tuple[str, int], tuple] = {}

    def prep(inst: str) -> h.Prep:
        if inst not in preps:
            preps[inst] = h.prepare(inst, tfs=("3m", "5m", "10m", "15m", "30m", "60m", "240m"))
        return preps[inst]

    def atr_of(inst: str, tf: int) -> tuple:
        if (inst, tf) not in atrs:
            p = prep(inst)
            atrs[inst, tf] = my_atr(p.ns, p.h, p.l, p.c, tf)
        return atrs[inst, tf]

    picked: list[tuple] = []  # (cell, inst, j, row, my atr)
    total = same = 0
    mismatches: list[str] = []
    for c in CELLS_B:
        tf, k, ext = parse_cell(c)
        rows = own[own.cell == c].reset_index(drop=True)
        sel = rng.choice(len(rows), size=300, replace=False)
        ok_c = 0
        for r in rows.iloc[sel].itertuples():
            p = prep(r.inst)
            j = int(np.searchsorted(p.ns, int(r.e0) * 1_000_000_000))
            assert p.ns[j] == int(r.e0) * 1_000_000_000, "entry minute not found"
            assert int(r.side) in (1, -1)
            cn, an = atr_of(r.inst, tf)
            a = atr_at(cn, an, int(p.ns[j]))
            bar, px, code, how = resim(p, j, int(r.side), k * a, ext)
            ok = (
                int(p.ns[bar] // 1_000_000_000) == int(r.x0)
                and code == int(r.why)
                and abs(px - float(r.level)) <= 1e-6 * abs(float(r.level))
                and how == r.how
            )
            total += 1
            same += ok
            ok_c += ok
            if not ok:
                mismatches.append(
                    f"   {c} {r.inst} e0={r.e0} side={r.side}: mine bar-time "
                    f"{p.ns[bar] // 1_000_000_000} px {px:.4f} code {code} {how} | study x0 "
                    f"{r.x0} px {float(r.level):.4f} code {r.why} {r.how} (my ATR {a:.4f})"
                )
            picked.append((c, r.inst, j, r, a, tf))
        say(f"cell {c}: {ok_c} of 300 match (exit minute, price, reason, open/touch)")
    for m in mismatches:
        say(m)
    say(f"B: {same} of {total} match" + ("" if same == total else f"; {total - same} differ"))
    reasons = pd.Series([int(x[3].why) for x in picked]).value_counts().sort_index()
    say(
        "   exit reasons in the sample (code: count): "
        + ", ".join(f"{k}: {v}" for k, v in reasons.items())
    )
    say()

    # my ATR against the study's own value (harness function ms.tf_age_atr), all 900 trades
    say("   my ATR against the study's value (ms.tf_age_atr) on the 900 trades:")
    worst = 0.0
    n_cmp = 0
    for inst in OWNER4:
        for tf in (15, 60):
            sub = [x for x in picked if x[1] == inst and x[5] == tf]
            if not sub:
                continue
            js = np.array([x[2] for x in sub])
            theirs = ms.tf_age_atr(prep(inst), f"{tf}m", js)[1]
            mine = np.array([x[4] for x in sub])
            worst = max(worst, float(np.max(np.abs(theirs - mine) / np.abs(theirs))))
            n_cmp += len(sub)
    say(f"   {n_cmp} trades compared, largest relative difference {worst:.2e}")
    say()

    say("=" * 100)
    say("C. LOOK-AHEAD TEST: ATR FROM ONE-MINUTE BARS CUT AT THE ENTRY MINUTE (50 TRADES)")
    say("=" * 100)
    pick = rng.choice(len(picked), size=50, replace=False)
    raw: dict[str, tuple] = {}
    worst = 0.0
    bad = 0
    shortest = 10**9
    for i in pick:
        c, inst, j, r, a_full, tf = picked[int(i)]
        if inst not in raw:
            b = h._cache.bars(h._cache.spec(inst).series_key, "1m")
            raw[inst] = (
                b.index.as_unit("ns").asi8,
                b["high"].to_numpy(float),
                b["low"].to_numpy(float),
                b["close"].to_numpy(float),
            )
        ns, hi, lo, cl = raw[inst]
        e_ns = int(r.e0) * 1_000_000_000
        cut = int(np.searchsorted(ns, e_ns, side="left"))  # bars strictly before the minute
        assert cut > 0 and ns[cut - 1] < e_ns
        shortest = min(shortest, cut)
        cn, an = my_atr(ns[:cut], hi[:cut], lo[:cut], cl[:cut], tf)
        # only bars complete by the entry minute count (the last one may be unfinished)
        a_cut = atr_at(cn, an, e_ns)
        diff = abs(a_cut - a_full) / a_full
        worst = max(worst, diff)
        bad += diff > 1e-9
    say(
        f"C: {50 - bad} of 50 equal, largest relative difference {worst:.2e} "
        f"(data cut to bars before the entry minute; shortest cut had {shortest} minute bars)"
    )
    say()


# ───────────────────────────── Part D ──────────────────────────────────────


def stock_part(sk: pd.DataFrame) -> None:
    say("=" * 100)
    say("D. STOCKS (PART C OF THE NOTE): FIGURES FROM stocks.csv AND 40 RE-SIMULATIONS")
    say("=" * 100)
    lines = NOTE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## Part C"))
    rows: dict[str, list[str]] = {}
    for ln in lines[start:]:
        if ln.startswith("|") and not ln.startswith("| cell") and not ln.startswith("|---"):
            cells = [norm(x) for x in ln.strip().strip("|").split("|")]
            rows[re.sub(r" \(.*\)$", "", cells[0])] = cells
    bad = 0
    for name, key in (("baseline", "baseline"), ("k3.0 ext kept", "daily k3.0 ext kept")):
        d = sk[sk.cell == key]
        f = figures(d)
        one = float(d.ret1.mean())
        before = float(d.net.mean())
        win_net = float((d.net > 0).mean() * 100)
        exc = (d.ret - d.bench).to_numpy()
        mine = {
            "n": str(f["n"]),
            "mean (t)": f"{f['mean']:+.2f} ({f['t']:+.1f})",
            "win": f"{f['win']:.1f}",
            "T/C/T": " / ".join(f"{v:+.2f}" for v in f["per"]),
            "one share": f"{one:+.2f}",
            "before DP": f"{before:+.2f}",
            "over hold (t)": f"{exc.mean():+.2f} ({day_t(exc, d['day'].to_numpy()):+.1f})",
        }
        r = rows[name]
        theirs = {"n": r[1], "mean (t)": r[2], "win": r[3], "T/C/T": r[7], "one share": r[9],
                  "before DP": r[10], "over hold (t)": r[11]}  # fmt: skip
        say(f"cell {name}  (winners before DP: {win_net:.1f}%)")
        for k in mine:
            ok = mine[k] == theirs[k]
            bad += not ok
            say(f"   {k:<14} mine {mine[k]:<20} note {theirs[k]:<20} {'ok' if ok else 'DIFFERENT'}")
    say(f"D figures: {'match' if bad == 0 else f'{bad} differ'}")

    # re-simulation of 40 random trades of k3.0 ext kept
    cost = h.LegCost.from_config(h._cost_cfg.product("equity_delivery"))
    slip = h.STOCK_SLIPPAGE
    d = sk[sk.cell == "daily k3.0 ext kept"].reset_index(drop=True)
    rng = np.random.default_rng(741)
    sel = rng.choice(len(d), size=40, replace=False)
    cache: dict[str, tuple] = {}
    same = 0
    notes: list[str] = []
    for r in d.iloc[sel].itertuples():
        if r.inst not in cache:
            key = h._cache.spec(r.inst).series_key
            dd = h._cache.bars(key, "1d")
            mm = h._cache.bars(key, "1m")
            cache[r.inst] = (
                np.array([x.date() for x in dd.index]),
                dd[["open", "high", "low", "close"]].to_numpy(float),
                np.array(mm.index.date),
                mm[["open", "high", "low"]].to_numpy(float),
            )
        dates, dly, mdates, mn = cache[r.inst]
        e = int(np.flatnonzero(np.array([x.toordinal() for x in dates]) == int(r.day))[0])
        t = e - 1
        o, hi, lo, cl = dly.T
        # ATR(14, Wilder, mean-of-14 seed) on daily bars up to the signal day t
        pc = np.r_[np.nan, cl[:t]]
        tr = np.maximum(hi[: t + 1] - lo[: t + 1],
                        np.maximum(np.abs(hi[: t + 1] - pc), np.abs(lo[: t + 1] - pc)))  # fmt: skip
        tr[0] = hi[0] - lo[0]
        acc = tr[:14].mean()
        for v in tr[14:]:
            acc = (acc * 13 + v) / 14
        buy = o[e] * (1 + slip)
        shares = max(1, round(1e4 / buy))
        rt = (cost.leg(np.array([buy]), False)[0] + cost.leg(np.array([buy]), True)[0]
              + 2 * slip * buy + h.DP_CHARGE_INR / shares)  # fmt: skip
        plan = R.plan_from_risk(1, buy, 3.0 * acc, 3.0, rt)
        prm = R.RatchetParams(extend=True)
        sma20 = pd.Series(cl).rolling(20).mean().to_numpy()
        n = len(cl)
        xr, at_close = n - 1, True
        for u in range(e, n):
            if cl[u] > sma20[u] or u - e + 1 >= 30 or cl[u] <= buy * 0.85:
                xr, at_close = (u + 1, False) if u + 1 < n else (u, True)
                break
        px_s = float(cl[xr] if at_close else o[xr])
        last = xr + 1 if at_close else xr
        st = R.start(plan)
        out = (xr, px_s, 5)
        done = False
        for u in range(e, last):
            sel_m = np.flatnonzero(mdates == dates[u])
            if len(sel_m):
                bars = mn[sel_m]
            else:
                bars = np.array([[o[u], hi[u], lo[u]]])
            for bo, bh, bl in bars:
                hit = R.exit_on_bar(plan, st, float(bo), float(bh), float(bl))
                if hit:
                    out = (u, float(hit[1]), CODE[hit[0].removeprefix("GAP_")])
                    done = True
                    break
                st, _ = R.advance(plan, st, float(bh), float(bl), prm)
            if done:
                break
        their_x = e + int(r.held)
        their_px = float(o[e] * (1 + r.bp / 1e4))
        ok = (
            out[0] == their_x
            and out[2] == int(r.why)
            and abs(out[1] - their_px) <= 1e-6 * abs(their_px)
        )
        same += ok
        if not ok:
            notes.append(f"   {r.inst} entry {dates[e]}: mine day {dates[out[0]]} px {out[1]:.4f} "
                         f"code {out[2]} | study day {dates[their_x]} px {their_px:.4f} "
                         f"code {r.why}")  # fmt: skip
    for m in notes:
        say(m)
    say(f"D re-simulation: {same} of 40 match (exit day, price, reason)")
    say()


# ───────────────────────────── Part E ──────────────────────────────────────


def part_e(ctl: pd.DataFrame) -> None:
    say("=" * 100)
    say("E. THE CONTROL (random entries, 20 seeds, four indices)")
    say("=" * 100)
    ctl = ctl[ctl.inst.isin(OWNER4)]
    positive = 0
    for c, g in ctl.groupby("cell", sort=False):
        m = g.groupby("seed").ret.mean()
        n = g.groupby("seed").size().mean()
        win = g.groupby("seed").ret.apply(lambda x: (x > 0).mean() * 100)
        positive += int((m > 0).sum())
        say(f"   {c:<22} seeds {len(m)}  trades/seed {n:.0f}  mean of seed means {m.mean():+.2f} "
            f"(sd {m.std(ddof=1):.2f}, {m.min():+.2f} to {m.max():+.2f})  pooled {g.ret.mean():+.2f}"
            f"  win {win.mean():.1f}  seeds positive {int((m > 0).sum())}")  # fmt: skip
    hold = ctl[ctl.cell == "hold to 15:15"].groupby("seed").ret.mean()
    say(
        f"E: seeds with a positive mean over all exits: {positive}; "
        f"hold to 15:15 mean of seed means {hold.mean():+.2f} (note: -1.15)"
    )
    say()


def main() -> int:
    h._init_worker()
    opt = pd.read_csv(OUT / "options.csv")
    part_a(opt)
    part_bc(opt)
    stock_part(pd.read_csv(OUT / "stocks.csv"))
    part_e(pd.read_csv(OUT / "control.csv"))
    (OUT / "check_report.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
