"""Stage 2: the two scoreboard strategies under the owner's multi-timeframe Supertrend logic.

Stage 1 (`mtf_bruteforce.py`) tried the bare rule "the timeframes agree, the line is the stop".
Stage 2 asks the question the owner put: do `supertrend_vol` and `supertrend_adx` earn anything
when they are run this way - Supertrend(14, 1.7), an entry timeframe E of 15m / 30m / 60m, the
strategy's own filter on the closed E bar, a multi-timeframe gate, and the Supertrend line as
the stop and the trail?

One structure =
    side x E x entry (flip <= 2 bars old | <= 4 | any age, once per trend | any age, once per
    trend and session) x filter (none | vol_a | vol_b | vol_c | adx_a | adx_b) x gate (E only |
    E+1d | E + every slower timeframe | all six | E + every faster timeframe) x trail line
    (E, or 60m / 1d when in the gate) x exit (touch | close) x entry-window end (11:30 | 13:00 |
    14:45) x horizon (intraday | carry).

The entry is the first minute at which everything is true, once per E trend - `first_since` of
the Lab strategies at one-minute resolution. Data, costs, the option model, the one-position
rule and the exits are stage 1's (same functions).

    .venv/bin/python scripts/mtf_stage2.py --validate
    .venv/bin/python scripts/mtf_stage2.py --run
    .venv/bin/python scripts/mtf_stage2.py --report

The finalists under stress and in depth are in `mtf_robust.py`.

Evidence about rules on past data, after modelled costs; not advice, not a forecast.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402

ROOT, OUT, TFS = h.ROOT, h.OUT, h.TFS
INTRADAY, CARRY, TOUCH, ALERT, CLOSE = h.INTRADAY, h.CARRY, h.TOUCH, h.ALERT, h.CLOSE

E_TFS = ("15m", "30m", "60m")
ENTRIES = ("flip<=2", "flip<=4", "any/once", "any/daily")
FILTERS = ("none", "vol_a", "vol_b", "vol_c", "adx_a", "adx_b")
#: supertrend_vol: (ADX minimum, RSI minimum for longs [100 - it for shorts], relative volume)
VOL_FILTERS = {"vol_a": (12.0, 55.0, 1.0), "vol_b": (18.0, 52.0, 1.0), "vol_c": (25.0, 60.0, 1.5)}
#: supertrend_adx: (ADX minimum, most ATRs away from VWAP); ADX rising over 3 bars, rvol >= 0.9
ADX_FILTERS = {"adx_a": (25.0, 3.0), "adx_b": (20.0, 1.5)}
GATES = ("E only", "E+1d", "E+slower", "all six", "E+faster")
WINDOW_ENDS = (11 * 60 + 30, 13 * 60, 14 * 60 + 45)
EXITS = (TOUCH, CLOSE)
HOLD_SESSIONS = (0, 1, 2, 3, 5, 8)
NO_FLIP = 10**6


def gate_masks(e: int) -> list[tuple[int, int]]:
    """(gate number, bit mask over TFS) for entry timeframe index `e`, duplicates removed."""
    n = len(TFS)
    raw = [
        1 << e,
        1 << e | 1 << (n - 1),
        sum(1 << i for i in range(e, n)),
        (1 << n) - 1,
        sum(1 << i for i in range(e + 1)),
    ]
    out, seen = [], set()
    for g, m in enumerate(raw):
        if m not in seen:
            seen.add(m)
            out.append((g, m))
    return out


def trail_lines(e: int, mask: int) -> list[int]:
    """The entry timeframe's own line, or the 60m / 1d line when that timeframe is in the gate."""
    return [e] + [t for t in (TFS.index("60m"), TFS.index("1d")) if t > e and mask >> t & 1]


def structures2() -> np.ndarray:
    """[side, E, entry, filter, gate, trail timeframe, exit mode, window, horizon]."""
    rows = []
    for side in (1, -1):
        for e_i, tf in enumerate(E_TFS):
            e = TFS.index(tf)
            for g, mask in gate_masks(e):
                for tr in trail_lines(e, mask):
                    for w_i in range(len(WINDOW_ENDS)):
                        for f_i in range(len(FILTERS)):
                            for en in range(len(ENTRIES)):
                                for xm in EXITS:
                                    for hz in (INTRADAY, CARRY):
                                        rows.append((side, e_i, en, f_i, g, tr, xm, w_i, hz))
    return np.asarray(rows, dtype=np.int16)


def name2(code) -> str:
    side, e_i, en, f_i, g, tr, xm, w_i, hz = (int(v) for v in code)
    end = WINDOW_ENDS[w_i]
    return (
        f"{'LONG ' if side == 1 else 'SHORT'} {E_TFS[e_i]} {ENTRIES[en]:<9} {FILTERS[f_i]:<5} "
        f"gate[{GATES[g]}] trail {TFS[tr]} {h.EXIT_MODES[xm]} <={end // 60}:{end % 60:02d} "
        f"{'carry' if hz == CARRY else 'intra'}"
    )


# ───────────────────────────── the entry timeframe ─────────────────────────


@dataclass
class EState:
    """What the last CLOSED bar of the entry timeframe says, for every one-minute bar."""

    seg: np.ndarray  # number of the E trend in force
    since: np.ndarray  # E bars since the flip that began it
    filt: dict[tuple[str, int], np.ndarray]  # (filter, side) -> the filter passes


def entry_state(p: h.Prep, tf: str) -> EState:
    from onelot.data.resample import resample_bars
    from onelot.indicators import core as ind

    bars = pd.DataFrame(
        {"open": p.o, "high": p.h, "low": p.l, "close": p.c, "volume": p.vol}, index=p.index
    )
    eb = resample_bars(bars, tf)
    trend = p.ladder[tf]
    if len(eb) != len(trend.direction):
        raise RuntimeError(f"{p.inst} {tf}: resample and ladder disagree")
    d = trend.direction.astype(np.int8)
    prev = np.r_[np.int8(0), d[:-1]]
    flip = (d != prev) & (prev != 0) & (d != 0)
    seg = np.cumsum(flip)
    k = np.arange(len(d))
    last = np.maximum.accumulate(np.where(flip, k, -1))
    since = np.where(last >= 0, k - last, NO_FLIP)

    hh, ll, cc = eb["high"], eb["low"], eb["close"]
    atr = ind.atr(hh, ll, cc, 14).to_numpy(float)
    rsi = ind.rsi(cc, 14).to_numpy(float)
    a = ind.adx(hh, ll, cc, 14)
    adx, pdi, mdi = (a[c].to_numpy(float) for c in ("adx", "plus_di", "minus_di"))
    hist = ind.macd(cc)["macd_hist"].to_numpy(float)
    vwap = ind.session_vwap(eb).fillna(ind.ema(cc, 20)).to_numpy(float)
    if float(eb["volume"].fillna(0).sum()) > 0:
        rv = ind.tod_rvol(eb["volume"], days=10).fillna(ind.rvol(eb["volume"], 20)).to_numpy(float)
    else:
        rv = np.full(len(eb), np.nan)  # an index: every volume filter is neutral, as in the Lab
    adx3 = np.r_[np.full(3, np.nan), adx[:-3]]
    hist1 = np.r_[np.nan, hist[:-1]]
    f: dict[tuple[str, int], np.ndarray] = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        dist = (cc.to_numpy(float) - vwap) / atr
        for side in (1, -1):
            f["none", side] = np.ones(len(d), dtype=bool)
            for name, (a_min, r_long, v_min) in VOL_FILTERS.items():
                r_ok = rsi >= r_long if side == 1 else rsi <= 100.0 - r_long
                f[name, side] = (adx >= a_min) & r_ok & ((rv >= v_min) | np.isnan(rv))
            for name, (a_min, far) in ADX_FILTERS.items():
                ok = (adx >= a_min) & (adx > adx3) & ((rv >= 0.9) | np.isnan(rv))
                if side == 1:
                    ok &= (pdi > mdi) & (hist > 0) & (hist >= hist1) & (dist > 0) & (dist <= far)
                else:
                    ok &= (mdi > pdi) & (hist < 0) & (hist <= hist1) & (dist < 0) & (dist >= -far)
                f[name, side] = ok

    pos = np.searchsorted(trend.close_ns, p.ns, side="right") - 1
    known = pos >= 0
    q = np.where(known, pos, 0)
    return EState(
        np.where(known, seg[q], -1),
        np.where(known, since[q], NO_FLIP),
        {key: known & v[q] for key, v in f.items()},
    )


def window(p: h.Prep, w_i: int) -> np.ndarray:
    return (p.minute >= h.ENTRY_START_MIN) & (p.minute <= WINDOW_ENDS[w_i]) & (p.year >= 0)


def gate_aligned(p: h.Prep, side: int, mask: int) -> np.ndarray:
    ds = p.dside(side)
    al = None
    for i in range(len(TFS)):
        if mask >> i & 1:
            al = ds[i].copy() if al is None else al & ds[i]
    return al


def pick_entries(p: h.Prep, es: EState, ok: np.ndarray, en: int) -> np.ndarray:
    """First allowed minute of each E trend (and, for `any/daily`, of each session in it)."""
    if en == 0:
        ok = ok & (es.since < 2)
    elif en == 1:
        ok = ok & (es.since < 4)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return idx
    k = es.seg[idx]
    new = np.r_[True, k[1:] != k[:-1]]
    if en == 3:
        s = p.sess[idx]
        new |= np.r_[True, s[1:] != s[:-1]]
    return idx[new]


def entries_for(p: h.Prep, es: EState, code) -> np.ndarray:
    side, e_i, en, f_i, g, tr, _xm, w_i, _hz = (int(v) for v in code)
    e = TFS.index(E_TFS[e_i])
    mask = dict(gate_masks(e))[g]
    with np.errstate(invalid="ignore"):
        ok = gate_aligned(p, side, mask) & (side * (p.o - p.L[tr]) > 0) & window(p, w_i)
    return pick_entries(p, es, ok & es.filt[FILTERS[f_i], side], en)


def trades_for(p: h.Prep, es: EState, code, delay=0, cost_mult=1.0, exit_mode=None) -> dict:
    side, tr, xm, hz = int(code[0]), int(code[5]), int(code[6]), int(code[8])
    if hz == CARRY and side == -1 and not p.is_option:
        return h._no_trades()
    E = entries_for(p, es, code)  # noqa: N806
    return h.trades_from(
        p, side, tr, xm if exit_mode is None else exit_mode, hz, E, delay, cost_mult
    )


def hold_trades(p: h.Prep, side: int, n_hold: int) -> dict:
    """No signal: enter every session at 09:30 and leave at the exit time `n_hold` sessions on."""
    if n_hold and side == -1 and not p.is_option:
        return h._no_trades()
    E = h.benchmark_entries(p, h.ENTRY_START_MIN)  # noqa: N806
    s = p.sess[E]
    tgt = s + n_hold
    ok = tgt < len(p.xi)
    E, s, tgt = E[ok], s[ok], tgt[ok]  # noqa: N806
    if p.is_option:
        tgt = np.minimum(tgt, p.exp_sess[s])
    X, PX = p.xi[tgt], p.xp[tgt]  # noqa: N806
    XT = p.ns[X] + np.where(p.xclose[tgt], h.NS_MIN, 0)  # noqa: N806
    out = h.pnl(p, side, CARRY if n_hold else INTRADAY, E, X, PX, XT)
    out.update({"E": E, "X": X, "PX": PX, "XT": XT, "hit": np.zeros(len(E), dtype=bool)})
    return out


BENCH = [(s, n) for s in (1, -1) for n in HOLD_SESSIONS]
BENCH_NAMES2 = [
    f"{'LONG ' if s == 1 else 'SHORT'} every session at 09:30, "
    + ("out at the close" if n == 0 else f"held {n} session{'s' if n > 1 else ''}")
    for s, n in BENCH
]


def grid2(p: h.Prep, codes: np.ndarray) -> dict[str, np.ndarray]:
    n_s, n_b = len(codes), len(BENCH)
    agg = np.zeros((n_s + n_b, 5, h.N_FIELDS))
    daysum = np.zeros((n_s + n_b, h.N_DAYS), dtype=np.float32)
    daycnt = np.zeros((n_s + n_b, h.N_DAYS), dtype=np.uint16)
    row = {tuple(int(v) for v in c): i for i, c in enumerate(codes)}
    wins = [window(p, w) for w in range(len(WINDOW_ENDS))]
    for e_i, tf in enumerate(E_TFS):
        e = TFS.index(tf)
        es = entry_state(p, tf)
        for side in (1, -1):
            for g, mask in gate_masks(e):
                al = gate_aligned(p, side, mask)
                for tr in trail_lines(e, mask):
                    with np.errstate(invalid="ignore"):
                        right = al & (side * (p.o - p.L[tr]) > 0)
                    for w_i, win in enumerate(wins):
                        base = right & win
                        for f_i, fname in enumerate(FILTERS):
                            okf = base & es.filt[fname, side]
                            for en in range(len(ENTRIES)):
                                E = pick_entries(p, es, okf, en)  # noqa: N806
                                for xm in EXITS:
                                    for hz in (INTRADAY, CARRY):
                                        if hz == CARRY and side == -1 and not p.is_option:
                                            continue
                                        t = h.trades_from(p, side, tr, xm, hz, E)
                                        i = row[(side, e_i, en, f_i, g, tr, xm, w_i, hz)]
                                        h.accumulate(p, t, agg[i], daysum[i], daycnt[i])
    for b, (side, n_hold) in enumerate(BENCH):
        i = n_s + b
        h.accumulate(p, hold_trades(p, side, n_hold), agg[i], daysum[i], daycnt[i])
    hold = np.zeros((5, 3))
    for yr in range(5):
        j = np.flatnonzero(p.year == yr)
        if len(j):
            hold[yr] = p.o[j[0]], p.c[j[-1]], len(np.unique(p.sess[j]))
    return {"agg": agg, "daysum": daysum, "daycnt": daycnt, "hold": hold}


def _run_instrument(task: tuple) -> tuple:
    inst, out_dir = task
    t0 = time.time()
    p = h.prepare(inst)
    codes = structures2()
    res = grid2(p, codes)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / f"{inst}.npz", codes=codes, **res)
    return inst, round(time.time() - t0, 1), int(res["agg"][: len(codes), :, h.F_N].sum())


def run(workers: int, only: list[str] | None, out_dir: Path) -> None:
    insts = [i for b in h.BASKETS.values() for i in b if not only or i in only]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=h._init_worker) as pool:
        futs = [pool.submit(_run_instrument, (i, str(out_dir))) for i in insts]
        for k, fut in enumerate(as_completed(futs), 1):
            inst, secs, n_tr = fut.result()
            print(f"[{k}/{len(insts)}] {inst:<16} {secs:>6.1f}s  {n_tr:>10,} trades "
                  f"(elapsed {time.time() - t0:.0f}s)", flush=True)  # fmt: skip
    print(f"total {time.time() - t0:.0f}s -> {out_dir}", flush=True)


# ───────────────────────────── validation ─────────────────────────────────


def validate() -> int:
    """Stage 2 against stage 1 where the two must agree, and the grid against the single path."""
    h._init_worker()
    rng = np.random.default_rng(12)
    codes = structures2()
    row = {tuple(int(v) for v in c): i for i, c in enumerate(codes)}
    full_window = len(WINDOW_ENDS) - 1
    bad = 0
    for inst in ("SBIN", "BANKNIFTY_OPT"):
        p = h.prepare(inst)
        states = {tf: entry_state(p, tf) for tf in E_TFS}
        # 1. no filter, no gate, any age, once per trend == stage 1's `[E] trail E ... once`
        n_cmp = n_tr = 0
        for e_i, tf in enumerate(E_TFS):
            e = TFS.index(tf)
            for side in (1, -1):
                for xm in EXITS:
                    for hz in (INTRADAY, CARRY):
                        if hz == CARRY and side == -1 and not p.is_option:
                            continue
                        code = (side, e_i, 2, 0, 0, e, xm, full_window, hz)
                        got = trades_for(p, states[tf], code)
                        eh = h.ENTRY_HORIZONS.index(("once", hz))
                        want = h.run_structure(p, (side, 1 << e, e, xm, eh))
                        same = (
                            len(got["E"]) == len(want["E"])
                            and np.array_equal(got["E"], want["E"])
                            and np.array_equal(got["X"], want["X"])
                            and np.allclose(got["ret"], want["ret"])
                        )
                        n_cmp += 1
                        n_tr += len(want["E"])
                        if not same:
                            bad += 1
                            print(
                                f"  MISMATCH {inst} {name2(code)}: {len(got['E'])} vs {len(want['E'])}"
                            )
        print(f"{inst}: {n_cmp} structures, {n_tr:,} trades: stage 2 == stage 1")
        # 2. the nested grid and the one-structure path give the same numbers
        res = grid2(p, codes)
        for i in rng.choice(len(codes), size=200, replace=False):
            t = trades_for(p, states[E_TFS[int(codes[i][1])]], codes[i])
            n_grid = int(res["agg"][i, :, h.F_N].sum())
            s_grid = float(res["agg"][i, :, h.F_SUM].sum())
            in_years = p.year[t["E"]] >= 0
            if (
                n_grid != int(in_years.sum())
                or abs(s_grid - float(t["ret"][in_years].sum())) > 1e-6
            ):
                bad += 1
                print(f"  MISMATCH grid vs single: {inst} {name2(codes[i])}")
        n_all = int(res["agg"][: len(codes), :, h.F_N].sum())
        print(
            f"{inst}: grid of {len(codes):,} structures ({n_all:,} trades) == single path on 200 samples"
        )
        # 3. a filter can only remove entries, a gate can only remove entries
        for _ in range(60):
            c = codes[rng.integers(len(codes))].copy()
            es = states[E_TFS[int(c[1])]]
            n_here = len(entries_for(p, es, c))
            loose = c.copy()
            loose[3] = 0  # no filter
            if tuple(int(v) for v in loose) in row and c[2] >= 2:
                # with "any age" the unfiltered rule fires at least once per trend the filtered one does
                seg_f = set(es.seg[entries_for(p, es, c)].tolist())
                seg_n = set(es.seg[entries_for(p, es, loose)].tolist())
                if not seg_f <= seg_n:
                    bad += 1
                    print(f"  MISMATCH filter adds trends: {inst} {name2(c)}")
            del n_here
    print("validation:", "PASS" if not bad else f"FAIL ({bad})")
    return int(bad > 0)


# ───────────────────────────── report ─────────────────────────────────────

HEAD2 = f"{'structure':<74}" + h.HEAD[58:]


def row2(name: str, st: dict, i: int) -> str:
    return h.row_text(f"{name:<74}", st, i)


def attrs2(codes: np.ndarray) -> dict[str, np.ndarray]:
    c = codes.astype(int)
    e_idx = np.array([TFS.index(E_TFS[v]) for v in c[:, 1]])
    return {
        "side": c[:, 0], "e": c[:, 1], "entry": c[:, 2], "filter": c[:, 3], "gate": c[:, 4],
        "trail_rel": np.where(c[:, 5] == e_idx, 0, np.where(c[:, 5] == TFS.index("60m"), 1, 2)),
        "exit": c[:, 6], "window": c[:, 7], "horizon": c[:, 8],
    }  # fmt: skip


def bench_row(st: dict, ns: int, side: int, sessions: float) -> int:
    n_hold = min(HOLD_SESSIONS, key=lambda v: abs(v - sessions))
    return ns + BENCH.index((side, n_hold))


def report_basket(b: h.Basket, w, csv_rows: list[dict]) -> None:
    st = h.period_stats(b)
    at = attrs2(b.codes)
    ns = b.n_struct
    n_rows = len(st["ALL"]["n"])
    n_tune, n_choose = h.MIN_TRADES[b.name]
    unit = "% of the premium paid" if b.name == "INDEX_OPTIONS" else "% of the share price"
    row = {tuple(int(v) for v in c): i for i, c in enumerate(b.codes)}

    def pad(x: np.ndarray, fill: int = -9) -> np.ndarray:
        return np.r_[x, np.full(n_rows - ns, fill)]

    w("=" * 150)
    w(f"{b.name}: {len(b.insts)} instruments, {ns:,} structures each. Net of modelled costs, {unit}, "
      "per trade. t clusters the trades entered on one day.")  # fmt: skip
    w("=" * 150)

    w("\n[1] No signal at all: one trade every session, every instrument")
    w(HEAD2)
    for k, name in enumerate(BENCH_NAMES2):
        if st["ALL"]["n"][ns + k]:
            w(row2(name, st, ns + k))

    # ---- the scoreboard strategies with their slot settings --------------------------
    w("\n[2] The scoreboard strategies as they are set today, run with Supertrend(14, 1.7) and the "
      "E line as stop and trail")  # fmt: skip
    if b.name == "INDEX_OPTIONS":
        fixed = [("supertrend_vol (index slot: 30m, flip<=2, ADX>=12, RSI>=55, entries to 13:00)", 1, 0, "vol_a", 1),
                 ("supertrend_adx (30m, flip<=4, ADX>=25 rising, DI, MACD, VWAP<=3 ATR, to 13:00)", 1, 1, "adx_a", 1)]  # fmt: skip
        sides = (1, -1)
    else:
        fixed = [("supertrend_vol (stock slot: 60m, flip<=4, ADX>=12, RSI<=45, entries to 11:30)", 2, 1, "vol_a", 0),
                 ("supertrend_adx (stock slot: 30m, flip<=4, ADX>=25 rising, DI, MACD, VWAP, to 11:30)", 1, 1, "adx_a", 0)]  # fmt: skip
        sides = (-1, 1)
    for title, e_i, en, fname, w_i in fixed:
        e = TFS.index(E_TFS[e_i])
        w(f"\n  {title}")
        w("  " + HEAD2)
        for side in sides:
            for g in (0, 3):
                for hz in (INTRADAY, CARRY):
                    for xm in EXITS:
                        i = row.get((side, e_i, en, FILTERS.index(fname), g, e, xm, w_i, hz))
                        if i is not None and st["ALL"]["n"][i]:
                            w("  " + row2(name2(b.codes[i]), st, i))

    # ---- what matters ---------------------------------------------------------------
    enough = (st["TUNE"]["n"] >= n_tune) & (st["CHOOSE"]["n"] >= n_choose) & (st["TEST"]["n"] >= n_choose)  # fmt: skip
    enough[ns:] = False
    w(f"\n[3] What matters, one property at a time (structures with at least {n_tune} trades in TUNE and "
      f"{n_choose} in CHOOSE and in TEST)")  # fmt: skip
    for hz, hz_name in ((INTRADAY, "INTRADAY"), (CARRY, "CARRY")):
        for side, s_name in ((1, "LONG"), (-1, "SHORT")):
            base = enough & (pad(at["horizon"]) == hz) & (pad(at["side"]) == side)
            if not base.any():
                continue
            w(f"\n  ── {hz_name} {s_name}: {int(base.sum()):,} structures ──")
            for title, key, names in (
                ("entry timeframe", "e", E_TFS), ("entry", "entry", ENTRIES),
                ("filter", "filter", FILTERS), ("gate", "gate", GATES),
                ("trail line", "trail_rel", ("E line", "60m line", "1d line")),
                ("exit", "exit", h.EXIT_MODES), ("entries until", "window", ("11:30", "13:00", "14:45")),
            ):  # fmt: skip
                groups = [(names[k], pad(at[key]) == k) for k in range(len(names))]
                groups = [g for g in groups if (g[1] & base).any()]
                h._marginal(w, title, groups, st, base)

    # ---- selection ------------------------------------------------------------------
    ok, score, checks = h.eligibility(b, st)
    w(
        f"\n[4] Selection by the rule written down before the run (TEST not used). {ns:,} structures tried."
    )
    running = np.ones(len(ok), dtype=bool)
    running[ns:] = False
    for label, v in checks.items():
        running &= v
        w(f"    after '{label}': {int(running.sum()):,} structures left")
    elig = np.flatnonzero(ok)
    beats = np.zeros(n_rows, dtype=bool)
    for i in elig:
        k = bench_row(st, ns, int(b.codes[i][0]), float(st["ALL"]["sess"][i]))
        beats[i] = all(
            st[pn]["mean"][i] > np.nan_to_num(st[pn]["mean"][k], nan=-1e9)
            for pn in ("TUNE", "CHOOSE")
        )
    if len(elig):
        order = elig[np.argsort(-score[elig])]
        tp = st["TEST"]["mean"][elig] > 0
        others = np.flatnonzero(~ok[:ns] & (st["TEST"]["n"][:ns] >= n_choose))
        w(f"    eligible: {len(elig):,}; beating the no-signal benchmark in TUNE and CHOOSE: {int(beats[elig].sum()):,}. "
          f"Positive in TEST: {int(tp.sum()):,} ({tp.mean() * 100:.0f}%) of the eligible against "
          f"{np.mean(st['TEST']['mean'][others] > 0) * 100:.0f}% of the {len(others):,} not eligible.")  # fmt: skip
        w(f"    median TEST mean of the eligible {np.median(st['TEST']['mean'][elig]):+.3f}%; of the top 20 "
          f"{np.median(st['TEST']['mean'][order[:20]]):+.3f}%")  # fmt: skip
        for hz, hz_name in ((INTRADAY, "intraday"), (CARRY, "carry")):
            for side, s_name in ((1, "long"), (-1, "short")):
                sub = order[(at["horizon"][order] == hz) & (at["side"][order] == side)]
                if not len(sub):
                    continue
                w(f"\n  Eligible {hz_name} {s_name}: {len(sub):,}; positive in TEST "
                  f"{np.mean(st['TEST']['mean'][sub] > 0) * 100:.0f}%, median TEST {np.median(st['TEST']['mean'][sub]):+.3f}%. "
                  "Top 10 by min(t TUNE, t CHOOSE) ('*' = beats the benchmark):")  # fmt: skip
                w("  " + HEAD2)
                for i in sub[:10]:
                    w("  " + row2(("*" if beats[i] else " ") + name2(b.codes[i]), st, i))
        detail2(b, st, int(order[0]), w, "The top-ranked structure")
    else:
        w("    eligible: 0 - no structure passes the rule.")

    # ---- the whole grid ---------------------------------------------------------------
    w("\n[5] The whole grid at a glance (structures with enough trades in every period)")
    pos = {pn: np.nan_to_num(st[pn]["mean"], nan=-1.0) > 0 for pn in ("TUNE", "CHOOSE", "TEST")}
    a3 = pos["TUNE"] & pos["CHOOSE"] & pos["TEST"]
    worst = np.minimum(np.minimum(st["TUNE"]["t"], st["CHOOSE"]["t"]), st["TEST"]["t"])
    for hz, hz_name in ((INTRADAY, "intraday"), (CARRY, "carry")):
        for side, s_name in ((1, "long"), (-1, "short")):
            m = enough & (pad(at["horizon"]) == hz) & (pad(at["side"]) == side)
            if not m.any():
                continue
            w(f"    {hz_name:<8} {s_name:<5} {int(m.sum()):>5,} structures: positive TUNE {pos['TUNE'][m].mean() * 100:.0f}%, "
              f"CHOOSE {pos['CHOOSE'][m].mean() * 100:.0f}%, TEST {pos['TEST'][m].mean() * 100:.0f}%, all three "
              f"{a3[m].mean() * 100:.1f}% ({int((a3 & m).sum()):,}); median ALL {np.median(st['ALL']['mean'][m]):+.3f}%, "
              f"median gross {np.median(st['ALL']['raw'][m]):+.3f}%")  # fmt: skip
            both = np.flatnonzero(a3 & m)
            if len(both):
                w("      positive in all three, best by the weakest period's t (found WITH the test year: a "
                  "description, not a selection):")  # fmt: skip
                w("      " + HEAD2)
                for i in both[np.argsort(-worst[both])][:8]:
                    w("      " + row2(name2(b.codes[i]), st, i))

    for i in range(ns):
        code = [int(v) for v in b.codes[i]]
        rec = {
            "basket": b.name, "side": "long" if code[0] == 1 else "short", "entry_tf": E_TFS[code[1]],
            "entry": ENTRIES[code[2]], "filter": FILTERS[code[3]], "gate": GATES[code[4]],
            "trail": TFS[code[5]], "exit": h.EXIT_MODES[code[6]],
            "entries_until": f"{WINDOW_ENDS[code[7]] // 60}:{WINDOW_ENDS[code[7]] % 60:02d}",
            "horizon": "carry" if code[8] == CARRY else "intraday",
            "eligible": bool(ok[i]), "beats_benchmark": bool(beats[i]), "score": round(float(score[i]), 3),
        }  # fmt: skip
        for pn in ("TUNE", "CHOOSE", "TEST", "ALL"):
            n = int(st[pn]["n"][i])
            rec[f"n_{pn}"] = n
            rec[f"mean_{pn}"] = round(float(st[pn]["mean"][i]), 4) if n else None
            rec[f"t_{pn}"] = round(float(st[pn]["t"][i]), 2) if n else None
        a = st["ALL"]
        if rec["n_ALL"]:
            rec.update({
                "win_pct": round(float(a["win"][i]), 1), "gross_pct": round(float(a["raw"][i]), 4),
                "sessions_held": round(float(a["sess"][i]), 2), "net_inr": round(float(a["inr"][i]), 0),
                "mean_with_dp": round(float(a["mean_dp"][i]), 4), "t_month": round(float(a["tm"][i]), 2),
            })  # fmt: skip
        csv_rows.append(rec)


def detail2(b: h.Basket, st: dict, i: int, w, title: str) -> None:
    agg = b.agg[i]
    a = st["ALL"]
    w(f"\n  {title}: {name2(b.codes[i])}")
    w(f"    ALL: {int(a['n'][i]):,} trades, mean {a['mean'][i]:+.3f}%, t by day {a['t'][i]:+.2f}, by month "
      f"{a['tm'][i]:+.2f}, win {a['win'][i]:.1f}%, profit factor {a['pf'][i]:.2f}, gross {a['raw'][i]:+.3f}%, net Rs "
      f"{a['inr'][i]:+,.0f}; ended by the line {a['event'][i]:.0f}%, held overnight {a['carried'][i]:.0f}%, mean "
      f"{a['sess'][i]:.1f} sessions")  # fmt: skip
    if b.name == "STOCKS" and a["carried"][i] > 0:
        w(f"    with the depository charge (Rs {h.DP_CHARGE_INR} a delivery sell, ONE share): mean "
          f"{a['mean_dp'][i]:+.3f}% (t {a['t_dp'][i]:+.2f})")  # fmt: skip
    w("    by year:  " + "  ".join(
        f"{h.YEAR_NAMES[y]} n {int(agg[y, h.F_N]):>4} {agg[y, h.F_SUM] / max(agg[y, h.F_N], 1):>+7.3f}%"
        for y in range(5)))  # fmt: skip
    cells = []
    for k, inst in enumerate(b.insts):
        ia = b.inst_agg[k, i]
        n = ia[:, h.F_N].sum()
        cells.append(
            f"{inst.replace('_OPT', '')} {ia[:, h.F_SUM].sum() / n:+.2f}" if n else f"{inst} -"
        )
    w("    by instrument (ALL, mean %): " + ", ".join(cells))


def report(out_dir: Path, dest: Path) -> None:
    lines: list[str] = []
    w = lines.append
    w("SCOREBOARD STRATEGIES UNDER THE MULTI-TIMEFRAME SUPERTREND(14, 1.7) LOGIC - STAGE 2")
    w(f"Bars: cleaned Dhan one-minute history {h.T0} .. {h.T_END}. TUNE = years 1-3, CHOOSE = year 4, "
      "TEST = year 5.")  # fmt: skip
    w("Evidence about rules on past data after modelled costs. Not advice and not a forecast.")
    csv_rows: list[dict] = []
    for name in h.BASKETS:
        w("")
        report_basket(h.load_basket(name, out_dir), w, csv_rows)
    dest.write_text("\n".join(lines) + "\n")
    pd.DataFrame(csv_rows).to_csv(dest.with_suffix(".csv"), index=False)
    print(f"{len(lines)} lines -> {dest}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default=str(OUT / "stage2"))
    ap.add_argument("--dest", default=str(OUT / "mtf_stage2.txt"))
    args = ap.parse_args()
    if args.validate:
        return validate()
    if args.run:
        run(args.workers, args.only, Path(args.out))
    if args.report:
        report(Path(args.out), Path(args.dest))
    if not (args.run or args.report):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
