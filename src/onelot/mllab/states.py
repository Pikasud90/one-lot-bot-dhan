"""Market states: each session is one of nine (direction x size), counted, never fitted.

A session's state uses its own bars and the 20 sessions before it, nothing later:
  z = ln(close_t / close_t-1) / sd(the 20 returns before t)  ->  bull (z > +0.5), bear
  (z < -0.5), else flat; range = (high - low) / close_t-1 against its 20-session median ->
  active (above), else calm. The first 21 sessions have no state.

The model is plain counting: base rates, first order (yesterday -> today) and second order
(the last two -> today) counts of the direction, first order counts of the size. A context's
probability is pulled toward the base rate when its count is small. Conviction is how far a
probability sits from the base rate in standard errors. It informs; it is never an alert.

This module imports only numpy, pandas and the standard library (the research script loads
it by file path).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

DIRS: tuple[str, ...] = ("bull", "flat", "bear")
SIZES: tuple[str, ...] = ("calm", "active")
WINDOW = 20
WARMUP = WINDOW + 1  # sessions without a state
Z_CUT = 0.5
PRIOR = 10.0  # pseudo-sessions of the base rate in every probability
SECOND_MIN_N = 60  # the second-order context is shown from this many cases
WALK_START = 250  # first stated session the walk-forward names
RECENT = 60
THIN_BELOW = 250  # fewer stated sessions than this: the page shows no evidence


# ── the state of a session ───────────────────────────────────────────────
def classify(daily: pd.DataFrame) -> pd.DataFrame:
    """Per session: r (log return), z, rng, direction, size. The first 21 rows have none.
    `daily` has open, high, low, close, oldest first; rows with a bad price are dropped."""
    cols = {c: pd.to_numeric(daily[c], errors="coerce") for c in ("high", "low", "close")}
    f = pd.DataFrame(cols).astype(float)
    f = f[np.isfinite(f).all(axis=1) & (f["close"] > 0) & (f["high"] >= f["low"])]
    prev = f["close"].shift(1)
    r = np.log(f["close"] / prev)
    # sd and median of the 20 sessions BEFORE t: shift the window by one
    s = r.shift(1).rolling(WINDOW).std(ddof=1)
    rng = (f["high"] - f["low"]) / prev
    m = rng.shift(1).rolling(WINDOW).median()
    z = r / s.where(s > 0)
    out = pd.DataFrame({"r": r, "z": z, "rng": rng, "rng_med": m}, index=f.index)
    stated = np.arange(len(out)) >= WARMUP
    stated &= out["z"].notna().to_numpy() & out["rng_med"].notna().to_numpy()
    d = np.where(out["z"] > Z_CUT, "bull", np.where(out["z"] < -Z_CUT, "bear", "flat"))
    sz = np.where(out["rng"] > out["rng_med"], "active", "calm")
    out["direction"] = pd.Series(d, index=out.index, dtype=object).where(stated, None)
    out["size"] = pd.Series(sz, index=out.index, dtype=object).where(stated, None)
    return out


def _stated(states: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    ok = states["direction"].notna().to_numpy()
    d = np.array([DIRS.index(x) for x in states["direction"].to_numpy()[ok]], dtype=int)
    s = np.array([SIZES.index(x) for x in states["size"].to_numpy()[ok]], dtype=int)
    return d, s


# ── counting ─────────────────────────────────────────────────────────────
class _Counts:
    """Direction and size counts over the stated sessions seen so far, in order."""

    def __init__(self) -> None:
        self.base = np.zeros(3)
        self.size_base = np.zeros(2)
        self.c1 = np.zeros((3, 3))
        self.c2 = np.zeros((3, 3, 3))
        self.s1 = np.zeros((2, 2))
        self.hist_d: list[int] = []
        self.hist_s: list[int] = []

    def add(self, d: int, s: int) -> None:
        if self.hist_d:
            self.c1[self.hist_d[-1], d] += 1
            self.s1[self.hist_s[-1], s] += 1
        if len(self.hist_d) >= 2:
            self.c2[self.hist_d[-2], self.hist_d[-1], d] += 1
        self.base[d] += 1
        self.size_base[s] += 1
        self.hist_d.append(d)
        self.hist_s.append(s)


def _base(counts: np.ndarray) -> np.ndarray:
    tot = counts.sum()
    return counts / tot if tot > 0 else np.full(len(counts), 1 / len(counts))


def _probs(row: np.ndarray, base: np.ndarray) -> np.ndarray:
    return (row + PRIOR * base) / (row.sum() + PRIOR)


def _conviction(edge: float, z: float) -> str:
    a = abs(z)
    if not math.isfinite(a) or a < 1:
        return "none"
    if a < 2:
        return "weak"
    if a < 3 or edge < 0.05:
        return "moderate"
    return "strong"


def _score(z: float) -> int:
    return int(min(100, round(25 * abs(z)))) if math.isfinite(z) else 0


def _lean(p: np.ndarray, base: np.ndarray, n: float) -> dict[str, Any]:
    i = int(np.argmax(p))
    edge = float(p[i] - base[i])
    se = math.sqrt(base[i] * (1 - base[i]) / n) if n > 0 and 0 < base[i] < 1 else float("nan")
    z = edge / se if se and math.isfinite(se) else 0.0
    return {
        "index": i,
        "edge": edge,
        "z": z,
        "conviction": _conviction(edge, z),
        "score": _score(z),
    }


def _next_dir(c: _Counts) -> dict[str, Any] | None:
    """The shown context for the session after the last one counted."""
    if not c.hist_d:
        return None
    base = _base(c.base)
    last1 = c.hist_d[-1]
    row1 = c.c1[last1]
    order, row, ctx = 1, row1, [last1]
    if len(c.hist_d) >= 2:
        row2 = c.c2[c.hist_d[-2], last1]
        if row2.sum() >= SECOND_MIN_N:
            order, row, ctx = 2, row2, [c.hist_d[-2], last1]
    n = float(row.sum())
    p = _probs(row, base)
    return {"order": order, "ctx": ctx, "n": n, "p": p, "base": base, **_lean(p, base, n)}


def _next_size(c: _Counts) -> dict[str, Any] | None:
    if not c.hist_s:
        return None
    base = _base(c.size_base)
    row = c.s1[c.hist_s[-1]]
    n = float(row.sum())
    p = _probs(row, base)
    out = _lean(p, base, n)
    # the page speaks of "active": the lean is calm or active, the edge is on that side
    return {"ctx": [c.hist_s[-1]], "n": n, "p": p, "base": base, **out}


def _counts_of(states: pd.DataFrame) -> _Counts:
    d, s = _stated(states)
    c = _Counts()
    for a, b in zip(d, s, strict=True):
        c.add(int(a), int(b))
    return c


def _round(x: float, k: int = 4) -> float:
    return round(float(x), k)


def _runs(d: np.ndarray) -> list[dict[str, Any]]:
    """After a run of exactly 1, exactly 2, and 3 or more sessions of a direction: how often
    the next session continued it."""
    out = []
    for name in ("bull", "bear"):
        di = DIRS.index(name)
        cont = {1: 0, 2: 0, 3: 0}
        tot = {1: 0, 2: 0, 3: 0}
        run = 0
        for i in range(len(d) - 1):
            run = run + 1 if d[i] == di else 0
            if run:
                k = min(run, 3)
                tot[k] += 1
                cont[k] += int(d[i + 1] == di)
        for k in (1, 2, 3):
            out.append(
                {
                    "direction": name,
                    "length": k,
                    "continued": _round(cont[k] / tot[k]) if tot[k] else None,
                    "n": tot[k],
                }
            )
    return out


def model(states: pd.DataFrame) -> dict[str, Any]:
    """Base rates, first and second order tables, the next-session reading and the runs."""
    c = _counts_of(states)
    if len(c.hist_d) < 3:
        return {"note": "too few stated sessions", "n_states": len(c.hist_d)}
    base = _base(c.base)
    sbase = _base(c.size_base)
    nxt = _next_dir(c)
    snx = _next_size(c)
    assert nxt is not None and snx is not None

    def table(counts: np.ndarray, ctxs: Iterable[tuple[int, ...]]) -> list[dict[str, Any]]:
        rows = []
        for ctx in ctxs:
            row = counts[ctx]
            p = _probs(row, base)
            rows.append(
                {
                    "context": [DIRS[i] for i in ctx],
                    "n": int(row.sum()),
                    "counts": {DIRS[i]: int(row[i]) for i in range(3)},
                    "probs": {DIRS[i]: _round(p[i]) for i in range(3)},
                }
            )
        return rows

    first = table(c.c1, [(i,) for i in range(3)])
    second = table(c.c2, [(i, j) for i in range(3) for j in range(3)])
    dirs_now = [DIRS[i] for i in c.hist_d[-2:]]
    li = nxt["index"]
    si = snx["index"]
    return {
        "n_states": len(c.hist_d),
        "base": {**{DIRS[i]: _round(base[i]) for i in range(3)}, "active": _round(sbase[1])},
        "last": dirs_now,
        "last_size": SIZES[c.hist_s[-1]],
        "next": {
            "context": [DIRS[i] for i in nxt["ctx"]],
            "order": nxt["order"],
            "n": int(nxt["n"]),
            "probs": {DIRS[i]: _round(nxt["p"][i]) for i in range(3)},
            "lean": DIRS[li],
            "edge_pts": _round(100 * nxt["edge"], 2),
            "z": _round(nxt["z"], 2),
            "conviction": nxt["conviction"],
            "score": nxt["score"],
        },
        "size_next": {
            "context": [SIZES[i] for i in snx["ctx"]],
            "n": int(snx["n"]),
            "p_active": _round(snx["p"][1]),
            "base_active": _round(sbase[1]),
            "lean": SIZES[si],
            # the edge of the leaning side over its own usual rate, in points
            "edge_pts": _round(100 * snx["edge"], 2),
            "z": _round(snx["z"], 2),
            "conviction": snx["conviction"],
            "score": snx["score"],
        },
        "first_order": first,
        "second_order": second,
        "runs": _runs(np.array(c.hist_d)),
    }


# ── the walk-forward check ───────────────────────────────────────────────
def walk_steps(states: pd.DataFrame, start: int = WALK_START) -> pd.DataFrame:
    """For each stated session from the `start`-th on: what the model, fitted on the sessions
    BEFORE it only, named, and what happened. One row per session."""
    ok = states["direction"].notna().to_numpy()
    dates = states.index[ok]
    d, s = _stated(states)
    rows = []
    c = _Counts()
    for j in range(len(d)):
        if j >= start:
            nd, ns = _next_dir(c), _next_size(c)
            assert nd is not None and ns is not None
            naive_d = int(np.argmax(c.base))
            naive_s = int(np.argmax(c.size_base))
            rows.append(
                {
                    "date": dates[j],
                    "lean": nd["index"],
                    "conviction": nd["conviction"],
                    "score": nd["score"],
                    "edge": nd["edge"],
                    "actual": int(d[j]),
                    "naive": naive_d,
                    "size_lean": ns["index"],
                    "size_conviction": ns["conviction"],
                    "size_actual": int(s[j]),
                    "size_naive": naive_s,
                }
            )
        c.add(int(d[j]), int(s[j]))
    return pd.DataFrame(rows)


def walk_forward(states: pd.DataFrame) -> dict[str, Any]:
    w = walk_steps(states)
    if w.empty:
        return {
            "n": 0,
            "hit": None,
            "hit_naive": None,
            "size_hit": None,
            "size_hit_naive": None,
        }
    return {
        "n": len(w),
        "hit": _round((w["lean"] == w["actual"]).mean()),
        "hit_naive": _round((w["naive"] == w["actual"]).mean()),
        "size_hit": _round((w["size_lean"] == w["size_actual"]).mean()),
        "size_hit_naive": _round((w["size_naive"] == w["size_actual"]).mean()),
    }


# ── one instrument from the desk's daily bars ────────────────────────────
def _sessions(states: pd.DataFrame) -> list[dict[str, Any]]:
    tail = states[states["direction"].notna()].tail(RECENT)
    return [
        {
            "date": str(pd.Timestamp(ts).date()),
            "r_pct": _round(100 * row.r, 3),
            "z": _round(row.z, 2),
            "direction": row.direction,
            "size": row["size"],
        }
        for ts, row in tail.iterrows()
    ]


def reading(daily: pd.DataFrame) -> dict[str, Any]:
    """Everything the page shows for one instrument, strict-JSON safe."""
    st = classify(daily)
    out: dict[str, Any] = {"as_of": None, "sessions": [], "note": None}
    got = model(st)
    out["thin"] = int(st["direction"].notna().sum()) < THIN_BELOW
    if "note" in got:
        out.update(got)
        out["note"] = "Too little history: market states need about 25 sessions at the least."
        return out
    stated = st[st["direction"].notna()]
    out.update(got)
    out["as_of"] = str(pd.Timestamp(stated.index[-1]).date())
    out["sessions"] = _sessions(st)
    out["walk_forward"] = walk_forward(st)
    if out["walk_forward"]["n"] == 0:
        out["note"] = (
            f"Only {got['n_states']} stated sessions: the check on past sessions needs "
            f"{WALK_START + 1}."
        )
    return out


class MarketStates:
    """`reading(key)`: the market states of one desk instrument, cached until that
    instrument's last closed daily bar changes. Read-only; never an alert."""

    def __init__(
        self,
        bars_getter: Callable[[str], tuple[pd.DataFrame | None, pd.DataFrame | None]],
        instruments: Callable[[], Iterable[dict[str, Any]]] | Iterable[dict[str, Any]],
        clock: Callable[[], dt.datetime],
        session_close: Callable[[dt.date], dt.datetime] | None = None,
        closed_bars: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        if closed_bars is None:
            from .service import closed_bars as closed_bars_
        else:
            closed_bars_ = closed_bars
        self._closed = closed_bars_
        self.bars_getter = bars_getter
        self._instruments = instruments
        self.clock = clock
        self.session_close = session_close
        self._cache: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.runs = 0  # readings computed (for tests)

    def instruments(self) -> list[dict[str, Any]]:
        src = self._instruments() if callable(self._instruments) else self._instruments
        rows = [
            {"key": str(r["key"]), "label": r.get("label") or str(r["key"]), "kind": r.get("kind")}
            for r in src
        ]
        return sorted(rows, key=lambda r: 0 if r["kind"] == "INDEX" else 1)  # stable

    def reading(self, key: str) -> dict[str, Any]:
        head = next((r for r in self.instruments() if r["key"] == key), None)
        if head is None:
            raise KeyError(key)
        with self._lock:
            try:
                fine, daily = self.bars_getter(key)
                bars = self._closed(fine, daily, "1d", self.clock(), self.session_close)
            except Exception as exc:  # noqa: BLE001 - one instrument, never the page
                log.exception("market states: bars of %s failed", key)
                return {**head_payload(head), "note": f"bars unavailable: {type(exc).__name__}"}
            if bars is None or bars.empty or not {"high", "low", "close"} <= set(bars.columns):
                return {**head_payload(head), "note": "No closed daily bars yet."}
            stamp = (int(pd.Timestamp(bars.index[-1]).value), len(bars))
            hit = self._cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
            try:
                body = {**head_payload(head), **reading(bars)}
            except Exception as exc:  # noqa: BLE001
                log.exception("market states: %s failed", key)
                return {**head_payload(head), "note": f"failed: {type(exc).__name__}"}
            self.runs += 1
            self._cache[key] = (stamp, body)
            return body


def head_payload(head: dict[str, Any]) -> dict[str, Any]:
    return {
        "instrument": {"key": head["key"], "label": head["label"], "kind": head["kind"]},
        "as_of": None,
        "sessions": [],
        "note": None,
    }
