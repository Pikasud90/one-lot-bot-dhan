"""US-099 verifier: does the product trade the rule the study tested? Read-only, own code.

    PYTHONPATH=src .venv/bin/python scripts/pullback_wide_check.py \
        --daily "MAIN/data/research/swing_2026-11-24/daily.sqlite" \
        --store "MAIN/data/onelot.db"

Three sources of trades are compared stock by stock and signal date, by entry date, exit date
and exit reason:

  study    trades_P1.pkl next to the daily file (the reasons recomputed from the bars here);
  mine     a loop written in this file from the rule's text (docs/research/SWING_UNIVERSE),
           on bars repaired here (own list of five corporate actions), not the product's code;
  product  `onelot.delivery.wide.WideDesk.run_round`, called here once per session by this
           file's own loop, on a temporary book, no adapter, no network.

NIFTY (R1 b) is the store's daily closes (opened mode=ro). Before the store's 200th NIFTY
session the study used Yahoo closes this verifier may not read: there R1(b) is forced true for
"mine" and for the product, so the study's trades must be found among theirs and the rest are
"only there" (a NIFTY-down day the study excluded). Sections 4 to 6 check sizes, costs and the
two tiers on the product's own book. Nothing is written but a temporary database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

import onelot.delivery.wide as wide_mod  # noqa: E402
from onelot.clock import IST, SessionClock, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.delivery.money import delivery_model, depository_paise  # noqa: E402
from onelot.delivery.rule import PullbackParams  # noqa: E402
from onelot.delivery.universe import UniverseStock, load_universe, stamped  # noqa: E402
from onelot.delivery.wide import RELEASES, StockDays, WideDesk  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402

LAST = dt.date(2026, 11, 7)
# own copy of the study's five unadjusted corporate actions (stage `check` of the study)
ACTIONS = {
    "ADANIENT": "2015-06-03",
    "JSL": "2015-11-19",
    "TATACHEM": "2020-03-04",
    "BRITANNIA": "2010-03-08",
    "TMPV": "2011-01-03",
}
DEPTH, LOOKBACK, TREND, EXIT_N, MAX_S, STOP, SLIP = 0.08, 20, 200, 20, 30, 0.15, 0.0003


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)


def load_bars(daily: Path, keys: list[str]) -> dict[str, pd.DataFrame]:
    """Daily bars per stock up to LAST: positive prices only, the five actions rescaled (own)."""
    con = ro(daily)
    out = {}
    for k in keys:
        rows = con.execute(
            "SELECT date, open, high, low, close, volume FROM daily WHERE symbol=? ORDER BY date",
            (k,),
        ).fetchall()
        f = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        f.index = [dt.date.fromisoformat(d) for d in f.pop("date")]
        f = f.astype(float)
        f = f[(f[["open", "high", "low", "close"]] > 0).all(axis=1)]
        if k in ACTIONS and dt.date.fromisoformat(ACTIONS[k]) in f.index:
            i = list(f.index).index(dt.date.fromisoformat(ACTIONS[k]))
            ratio = f["close"].iloc[i] / f["close"].iloc[i - 1]
            f.iloc[:i, :4] = f.iloc[:i, :4].to_numpy() * ratio
            f.iloc[:i, 4] = f.iloc[:i, 4].to_numpy() / ratio
        out[k] = f[[d <= LAST for d in f.index]]
    con.close()
    return out


def load_nifty_cache(store: Path) -> pd.Series:
    """NIFTY's daily closes as the study read them: the lab's DataCache (cleaned bars)."""
    from onelot.lab.runner import DataCache

    uri = f"file:{quote(str(store.resolve()))}?mode=ro"
    eng = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    b = DataCache(engine=eng).bars("NIFTY", "1d")
    idx = pd.DatetimeIndex(b.index)
    idx = idx.tz_convert(IST) if idx.tz is not None else idx
    s = pd.Series(b["close"].to_numpy(float), index=list(idx.date))
    s = s[~s.index.duplicated(keep="last")]
    return s[[d <= LAST for d in s.index]]


def load_nifty(store: Path) -> pd.Series:
    con = ro(store)
    rows = con.execute(
        "SELECT ts, close FROM bars WHERE series_key='NIFTY' AND resolution='1d' ORDER BY ts"
    ).fetchall()
    con.close()
    s = pd.Series([r[1] for r in rows], index=[dt.date.fromisoformat(r[0][:10]) for r in rows])
    s = s[~s.index.duplicated(keep="last")]
    return s[[d <= LAST for d in s.index]].astype(float)


# ── the rule, written from its text ───────────────────────────────────────────────────────
def reasons_at(c: np.ndarray, e: int, fill: float, u: int) -> str:
    """R3 on the close of u for a trade that entered on bar e, in R3's order."""
    out = []
    if u + 1 >= EXIT_N and c[u] > float(np.mean(c[u + 1 - EXIT_N : u + 1])):
        out.append("SMA20")
    if u - e + 1 >= MAX_S:
        out.append("TIME")
    if c[u] <= fill * (1 - STOP):
        out.append("STOP")
    return ",".join(out)


def my_trades(
    dates: list[dt.date], o: np.ndarray, c: np.ndarray, ok: dict[dt.date, bool]
) -> list[dict[str, Any]]:
    n = len(c)
    mean200 = pd.Series(c).rolling(TREND).mean().to_numpy()
    out = []
    t = TREND  # the first index with 200 closes before it: 201 bars
    while t < n - 1:
        hi = c[t - LOOKBACK + 1 : t + 1].max()
        hi_before = c[t - LOOKBACK : t].max()
        deep = c[t] <= (1 - DEPTH) * hi
        deep_before = c[t - 1] <= (1 - DEPTH) * hi_before
        if not (c[t] > mean200[t] and ok.get(dates[t], False) and deep and not deep_before):
            t += 1
            continue
        e = t + 1
        fill = o[e] * (1 + SLIP)
        rec = {"signal": dates[t], "entry": dates[e], "exit": "open", "reason": ""}
        x = n - 1
        for u in range(e, n):
            r = reasons_at(c, e, fill, u)
            if r:
                rec["reason"] = r
                if u + 1 < n:
                    rec["exit"], x = dates[u + 1], u + 1
                else:
                    rec["exit"], x = "pending", u
                break
        out.append(rec)
        if rec["exit"] in ("open", "pending"):
            break
        t = x
    return out


# ── comparison ────────────────────────────────────────────────────────────────────────────
def compare(
    name: str,
    ref: dict[tuple[str, dt.date], dict[str, Any]],
    got: dict[tuple[str, dt.date], dict[str, Any]],
    bars: dict[str, pd.DataFrame],
    since: dt.date,
    nifty_from: dt.date,
    show: int,
) -> dict[str, int]:
    """Count matches and every kind of difference, `ref` against `got`, signals from `since`."""
    kinds: dict[str, list[str]] = defaultdict(list)
    match = {"after": 0, "before": 0}
    total = {"after": 0, "before": 0}
    held: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
    for (k, d), r in ref.items():
        if d < since:
            continue
        w = "after" if d >= nifty_from else "before"
        total[w] += 1
        g = got.get((k, d))
        tag = "" if w == "after" else " [signal before NIFTY is readable]"
        if g is None:
            held[k].append((r["entry"], r["exit"]))
            busy = any(
                v["entry"] <= d <= (v["exit"] if v["exit"] not in ("open", "pending") else LAST)
                for (kk, _), v in got.items()
                if kk == k and v["entry"] is not None
            )
            if busy:
                tag += " [got was in another trade of the stock that day]"
            kinds["only in ref, not in got" + tag].append(f"{k} {d} -> {r['exit']} {r['reason']}")
            continue
        if g["entry"] != r["entry"]:
            kinds["entry date differs" + tag].append(f"{k} {d}: {r['entry']} / {g['entry']}")
        elif g["exit"] != r["exit"]:
            kind = "exit date differs"
            f = bars[k]
            dl = list(f.index)
            if "STOP" in (r["reason"] + g["reason"]) and r["exit"] not in ("open", "pending"):
                u = dl.index(r["exit"]) - 1
                fill = float(f["open"].iloc[dl.index(r["entry"])]) * (1 + SLIP)
                booked = round(fill * 100) / 100
                cl = float(f["close"].iloc[u])
                if booked * (1 - STOP) < cl <= fill * (1 - STOP):
                    kind += " (stop read on a fill rounded to the paisa)"
            kinds[kind + tag].append(
                f"{k} {d}: {r['exit']} {r['reason']} / {g['exit']} {g['reason']}"
            )
        elif g["reason"] != r["reason"]:
            kind = "exit reason differs"
            f = bars[k]
            if r["exit"] not in ("open", "pending"):
                u = list(f.index).index(r["exit"]) - 1
                cl = f["close"].to_numpy(float)
                if abs(cl[u] - float(np.mean(cl[u - 19 : u + 1]))) <= 1e-9 * cl[u]:
                    kind += " (close equals its SMA20 but for rounding noise)"
            kinds[kind + tag].append(f"{k} {d}: {r['reason'] or 'none'} / {g['reason'] or 'none'}")
        else:
            match[w] += 1
    for (k, d), g in sorted(got.items()):
        if d >= since and (k, d) not in ref:
            kind = "only in got, not in ref"
            if d < nifty_from:
                kind += " [signal before NIFTY is readable: forced true]"
            elif any(e <= d <= (x if x not in ("open", "pending") else LAST) for e, x in held[k]):
                kind += " [ref still held a trade it has and got lacks]"
            kinds[kind].append(f"{k} {d} -> {g['exit']} {g['reason']}")
    print(f"--- {name}")
    print(
        f"  signals from {since}: ref {total['after'] + total['before']} "
        f"(readable NIFTY: {total['after']}, before: {total['before']})"
    )
    print(
        f"  matched by entry, exit, reason: {match['after'] + match['before']} "
        f"(readable NIFTY: {match['after']} of {total['after']}; "
        f"before: {match['before']} of {total['before']})"
    )
    for kind, rows in sorted(kinds.items(), key=lambda x: -len(x[1])):
        print(f"  MISMATCH {kind}: {len(rows)}")
        for row in rows[:show]:
            print(f"      {row}")
    return {"match": sum(match.values()), "total": sum(total.values())}


def study_records(
    tr: pd.DataFrame, bars: dict[str, pd.DataFrame]
) -> dict[tuple[str, dt.date], dict[str, Any]]:
    out = {}
    for r in tr.itertuples():
        f = bars[r.stock]
        dl = list(f.index)
        c = f["close"].to_numpy(float)
        sig, ent, ex = (pd.Timestamp(x).date() for x in (r.signal_date, r.entry_date, r.exit_date))
        e = dl.index(ent)
        fill = float(f["open"].iloc[e]) * (1 + SLIP)
        if r.open_at_end:
            reason = reasons_at(c, e, fill, len(c) - 1)
            out[(r.stock, sig)] = {
                "entry": ent, "exit": "pending" if reason else "open", "reason": reason,
            }  # fmt: skip
        else:
            out[(r.stock, sig)] = {
                "entry": ent, "exit": ex, "reason": reasons_at(c, e, fill, dl.index(ex) - 1),
            }  # fmt: skip
    return out


def product_records(book: Any) -> dict[tuple[str, dt.date], dict[str, Any]]:
    out = {}
    for t in book.all():
        if t.status == "CLOSED":
            ex = t.exit_session
        elif t.status == "CANCELLED":  # US-103: an entry never filled, not an open trade
            ex = "cancelled"
        elif t.status == "EXIT_PENDING":
            ex = "pending"
        else:
            ex = "open"
        out[(t.series_key, t.signal_date)] = {
            "entry": t.entry_session, "exit": ex, "reason": t.exit_reason or "",
        }  # fmt: skip
    return out


def make_desk(
    stocks: list[UniverseStock], params: PullbackParams, cfg: Any
) -> tuple[WideDesk, Path]:
    costs = CostConfig.load(ROOT / "config" / "costs.yaml")
    cal = TradingCalendar.from_yaml(ROOT / "config" / "nse_holidays.yaml")
    tmp = Path(tempfile.mkdtemp(prefix="wide-check-")) / "book.db"
    desk = WideDesk(
        create_engine(f"sqlite:///{tmp}"), stocks, params, delivery_model(costs),
        depository_paise(costs), cal, SessionClock(SessionCfg(), cal),
        cap_open=cfg.wide.cap_open, dormant_after=cfg.wide.dormant_after,
        min_bars=cfg.delivery.min_bars, background=False,
    )  # fmt: skip
    return desk, tmp


def run_product(
    keys: list[str],
    groups: dict[str, str],
    bars: dict[str, pd.DataFrame],
    nifty: pd.Series,
    nifty_from: dt.date,
    params: PullbackParams,
    cfg: Any,
) -> tuple[WideDesk, dict[tuple[str, dt.date], dict[str, Any]]]:
    """My own loop: one `run_round` per session over `keys`' bars, R1(b) forced before
    `nifty_from` (a synthetic NIFTY of constant closes stands for the days the store lacks)."""
    stocks = [UniverseStock(k, k, k, "0", "NSE_EQ", groups[k]) for k in keys]
    desk, _ = make_desk(stocks, params, cfg)
    days = {k: StockDays.of(k, stamped(bars[k])) for k in keys}
    sessions = sorted({d for k in keys for d in bars[k].index})
    pre = [d for d in sessions if d < nifty.index[0]]
    n_dates = pre + list(nifty.index)
    n_close = np.concatenate([np.ones(len(pre)), nifty.to_numpy(float)])
    n_ord = np.array([d.toordinal() for d in n_dates])
    original = wide_mod.nifty_ok

    def forced(dates: Any, nd: Any, nc: Any, p: Any) -> np.ndarray:
        got = original(dates, nd, nc, p)
        early = np.array([d < nifty_from for d in dates], dtype=bool)
        return got | early

    wide_mod.nifty_ok = forced  # type: ignore[assignment]
    desk._fetch = None
    desk._source = lambda ks: days
    try:
        for s in sessions:
            k = int(np.searchsorted(n_ord, s.toordinal(), side="right"))
            desk._nifty = lambda now, k=k: (n_dates[:k], n_close[:k])  # type: ignore[misc]
            desk.run_round(dt.datetime.combine(s, dt.time(18, 0), tzinfo=IST), s)
    finally:
        wide_mod.nifty_ok = original  # type: ignore[assignment]
    return desk, product_records(desk.book)


# ── sections 4 to 6 ───────────────────────────────────────────────────────────────────────
def paise(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def leg_by_hand(turnover: Decimal, side: str) -> Decimal:
    """Rupees of one delivery leg from the cost table's rates, written out here."""
    brokerage = min(Decimal(20), Decimal("0.0003") * turnover)
    stt = Decimal("0.001") * turnover
    exch = Decimal("0.0000297") * turnover
    sebi = Decimal("0.000001") * turnover
    ipft = Decimal("0.000001") * turnover
    stamp = Decimal("0.00015") * turnover if side == "buy" else Decimal(0)
    gst = Decimal("0.18") * (brokerage + exch + sebi + ipft)
    return brokerage + stt + exch + sebi + ipft + stamp + gst


def check_costs(desk: WideDesk, bars: dict[str, pd.DataFrame], n: int = 10) -> int:
    # the rule's own exits only: a release (US-103) has no bar on its exit session, and a
    # REVIEW release books no money
    closed = [t for t in desk.book.all() if t.status == "CLOSED" and t.exit_reason not in RELEASES]
    closed.sort(key=lambda t: (t.signal_date.toordinal() * 7919) % 1009)
    picks: list[Any] = []
    for want in ("STOP", "TIME", "SMA20"):
        picks += [t for t in closed if t.exit_reason == want and t not in picks][:3]
    expensive = [t for t in closed if t.signal_close > 10000 and t not in picks]
    picks = (expensive[:1] + picks + closed)[:n]
    bad = 0
    print(f"--- 4 sizes and costs of {len(picks)} trades, by hand (Decimal, the table's rates)")
    for t in picks:
        f = bars[t.series_key]
        o_in = Decimal(repr(float(f.loc[t.entry_session, "open"])))
        o_out = Decimal(repr(float(f.loc[t.exit_session, "open"])))
        qty = max(1, math.floor(10000 / t.signal_close + 0.5))
        pin = paise(o_in * Decimal("1.0003") * 100)
        pout = paise(o_out * Decimal("0.9997") * 100)
        chg = paise(leg_by_hand(Decimal(pin * qty) / 100, "buy") * 100) + paise(
            leg_by_hand(Decimal(pout * qty) / 100, "sell") * 100
        )
        net = (pout - pin) * qty - chg - paise(Decimal("14.75") * 100)
        pct = net / (pin * qty) * 100
        ok = (
            qty == t.qty and pin == t.entry_paise and pout == t.exit_paise
            and chg == t.charges_paise and net == t.net_paise and abs(pct - t.net_pct) < 1e-9
        )  # fmt: skip
        bad += not ok
        print(
            f"  {t.series_key:11s} {t.signal_date} {t.exit_reason:5s} qty {qty:4d}/{t.qty:<4d} "
            f"in {pin}/{t.entry_paise} out {pout}/{t.exit_paise} charges {chg}/{t.charges_paise} "
            f"net {net}/{t.net_paise} ({pct:+.3f}%) value Rs {pin * qty / 100:,.0f} "
            f"{'OK' if ok else 'DIFFERS'}"
        )
    return bad


def check_tiers(
    desk: WideDesk, bars: dict[str, pd.DataFrame], groups: dict[str, str], month: str, cap: int
) -> int:
    """The month's signals, the places they took, and an own replay of the admission order."""
    rows = desk.book.all()
    by_day: dict[dt.date, list[Any]] = defaultdict(list)
    for t in rows:
        by_day[t.signal_date].append(t)
    days = sorted(d for d in by_day if d.isoformat().startswith(month))
    inmonth = [t for d in days for t in by_day[d]]
    print(
        f"--- 6 tiers in {month}: {len(inmonth)} signals on {len(days)} days, "
        f"{sum(t.tier == 'cap' for t in inmonth)} within the cap of {cap}, "
        f"{sum(t.tier == 'test' for t in inmonth)} recorded only"
    )
    bad = 0
    for d in sorted(by_day):
        if not d.isoformat().startswith(month):
            continue
        day = by_day[d]
        busy = [
            t for t in rows
            if t.tier == "cap" and t.signal_date < d
            and (t.exit_session is None or t.exit_session > d)
        ]  # fmt: skip
        free = cap - len(busy)

        def depth(t: Any, d: dt.date = d) -> float:
            f = bars[t.series_key]
            c = f["close"].to_numpy(float)
            i = list(f.index).index(d)
            return (1 - c[i] / c[i - LOOKBACK + 1 : i + 1].max()) * 100

        order = sorted(day, key=lambda t: (groups[t.series_key] != "desk", -depth(t), t.series_key))
        want = {t.series_key for t in order[: max(0, free)]}
        got = {t.series_key for t in day if t.tier == "cap"}
        flag = "" if want == got else "  <-- DIFFERS from my own admission"
        bad += want != got
        print(
            f"  {d}: {len(day):3d} signals, {len(busy)} places in use, {max(free, 0)} free, "
            f"{len(got)} took a place{flag}"
        )
        if want != got or len(day) > max(free, 0):
            for t in order[:12]:
                print(
                    f"        {t.series_key:11s} {groups[t.series_key]:5s} depth {depth(t):5.2f} "
                    f"-> {t.tier}"
                )
    lives = [t for t in rows if t.tier not in ("cap", "test")]
    print(f"  trades with a tier other than cap or test: {len(lives)}")
    return bad


def pick_stocks(groups: dict[str, str], per: int = 10) -> list[str]:
    chosen: list[str] = []
    for g in ("desk", "alpha", "new"):
        ks = sorted(k for k, v in groups.items() if v == g)
        forced = [k for k in ks if k in ACTIONS]
        rest = [k for k in ks if k not in forced]
        need = max(0, per - len(forced))
        idx = np.linspace(0, len(rest) - 1, need).round().astype(int) if need else []
        chosen += forced + [rest[i] for i in sorted(set(idx))]
    return chosen


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daily", type=Path, required=True)
    ap.add_argument("--store", type=Path, required=True, help="the store, read only, for NIFTY")
    ap.add_argument("--since", type=dt.date.fromisoformat, default=dt.date(2015, 1, 1))
    ap.add_argument("--month", default="2020-03")
    ap.add_argument(
        "--nifty", choices=("raw", "cache"), default="raw",
        help="raw: the stored daily bars; cache: the lab's cleaned closes the study read",
    )  # fmt: skip
    ap.add_argument("--show", type=int, default=3)
    a = ap.parse_args()
    t0 = time.perf_counter()
    universe = load_universe(ROOT / "config" / "delivery_universe.yaml")
    groups = {s.key: s.group for s in universe.stocks}
    cfg = load_desk_config()
    params = PullbackParams.from_mapping(cfg.delivery_slots()[0][1].params)
    study = pd.read_pickle(a.daily.parent / "trades_P1.pkl")
    nifty = load_nifty_cache(a.store) if a.nifty == "cache" else load_nifty(a.store)
    nifty_from = nifty.index[TREND - 1]
    mean = nifty.rolling(TREND).mean()
    ok = {d: bool(v) for d, v in (nifty > mean).items()}
    print(
        f"NIFTY ({a.nifty}) {nifty.index[0]} to {nifty.index[-1]}; SMA200 readable from {nifty_from}"
    )

    sample = pick_stocks(groups)
    every = sorted(groups)
    bars = load_bars(a.daily, every)
    print(f"sample ({len(sample)}): {' '.join(sample)}")

    def mine(keys: list[str]) -> dict[tuple[str, dt.date], dict[str, Any]]:
        out = {}
        for k in keys:
            f = bars[k]
            okk = {d: (ok.get(d, False) or d < nifty_from) for d in f.index}
            for r in my_trades(
                list(f.index), f["open"].to_numpy(float), f["close"].to_numpy(float), okk
            ):
                out[(k, r["signal"])] = r
        return out

    for label, keys in (("30 sampled stocks", sample), ("all 222 stocks", every)):
        print(f"\n=== {label}")
        sub = study[study.stock.isin(keys)]
        ref = study_records(sub, bars)
        mine_rec = mine(keys)
        desk, prod = run_product(keys, groups, bars, nifty, nifty_from, params, cfg)
        print(f"trades: study {len(ref)}, mine {len(mine_rec)}, product {len(prod)}")
        compare("study vs product", ref, prod, bars, a.since, nifty_from, a.show)
        compare("study vs mine", ref, mine_rec, bars, a.since, nifty_from, a.show)
        compare("mine vs product", mine_rec, prod, bars, a.since, nifty_from, a.show)
        if label.startswith("all"):
            bad = check_costs(desk, bars)
            print(f"  cost lines that differ: {bad}")
            sig_by_month = Counter(t.signal_date.isoformat()[:7] for t in desk.book.all())
            busiest = [m for m, _ in sig_by_month.most_common(3)]
            print(
                "--- signals a month, the busiest:",
                ", ".join(f"{m} {sig_by_month[m]}" for m in busiest),
            )
            for m in dict.fromkeys([a.month, "2020-02", *busiest]):
                bad = check_tiers(desk, bars, groups, m, cfg.wide.cap_open)
                print(f"  days whose places differ from my own admission in {m}: {bad}")
            closed = [
                t
                for t in desk.book.all()
                if t.status == "CLOSED"
                and t.exit_reason not in RELEASES  # the rule's own exits (US-103)
                and t.signal_date >= a.since
            ]
            sc = study[[pd.Timestamp(x).date() >= a.since for x in study.signal_date]]
            sc = sc[~sc.open_at_end]
            print(
                f"--- 5 mean net % a trade from {a.since}: product (nearest-share size, paise "
                f"fills) {np.mean([t.net_pct for t in closed]):+.3f} on {len(closed)}; "
                f"study (floor size) {sc.net_pct.mean():+.3f} on {len(sc)}"
            )
    print(f"{time.perf_counter() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
