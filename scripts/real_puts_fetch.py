"""US-098: real near monthly index-option bars for the long-volatility question, one data fetch.

What it fetches (NIFTY and BANKNIFTY, the near monthly contract, Dhan's rolling series):
  tier 1  the at-the-money PUT of every weekday from 2021-11-08 to 2026-11-07;
  tier 2  the at-the-money CALL of every session the app's store lacks;
  tier 3+ the CALL and the PUT at the strike the pair was bought at, on the minute it is sold
          (the rolling series is relative to each minute's at-the-money strike, so a strike
          bought at the money at 09:20 is "ATM+r" at 15:20 once the index has moved):
          3 = 09:20 bought, 15:20 sold; 4 = 09:45 / 15:20; 5 = 09:20 / 12:00; 6 = 09:45 / 12:00;
          7 = the minutes a -30% / +60% rule sold at (a list from `long_vol_real.py stopneeds`).

Run from the MAIN checkout (its `src` and `scripts` give the app's adapter, store and
universe), this file from the US-098 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/real_puts_fetch.py" <stage> [options]

Stages
  probe    the fetcher proven on three requests (two of them days the store already holds,
           compared bar by bar); at most FIVE requests in all before 15:40 IST.
  plan     write the needs of every tier the data allows into the ledger (run again after a
           fetch: tier 2 needs tier 1, tiers 3 to 6 need the at-the-money bars).
           `plan --stops FILE` adds tier 7 from a CSV (underlying, opt, rel, day).
  fetch    the needs in tier order, at most ONE request a second, only after 15:40 IST, until
           21:15 IST; stops after three refusals in a row. Resumable: a day stored (here or in
           the app's store) is never asked again, and one request stores two days.
  status   requests sent, needs left per tier, blocks held.

Writes only into `data/research/real_puts_2026-11-25/` of the main checkout: `options.sqlite`
(the app's `OptionBarStore`, the same schema as `data/expired_options.db`, so it can be merged
with `scripts/merge_option_bars.py`) and `ledger.sqlite` (every request sent, and the needs).
The app's store is opened read only (SQLite `mode=ro`). Credentials are read by the app's own
`Secrets.load`, never printed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from onelot.clock import IST
from onelot.data.expired_options import (
    Block,
    OptionBarStore,
    RollingKey,
    Underlying,
    _unpack,
    fetch_days,
)

SERIES = ("NIFTY", "BANKNIFTY")
FIRST, LAST = dt.date(2021, 11, 8), dt.date(2026, 11, 7)
STUDY = Path("data") / "research" / "real_puts_2026-11-25"
OPEN_GATE = dt.time(15, 40)  # the bulk only after the market and the desk's close work
DEADLINE = dt.time(21, 15)  # the token expires at about 21:30 IST
MAX_BEFORE_GATE = 5
MIN_GAP_S = 1.0
MAX_FAILS_IN_A_ROW = 3
MAX_TRIES = 2  # a need refused twice is left alone
MAX_WAIT = 3  # minutes: the first traded minute within 3 of the wanted one
ENTRIES = {"09:20": 560, "09:45": 585}
EXITS = {"15:20": 920, "12:00": 720}
TIER_OF = {("09:20", "15:20"): 3, ("09:45", "15:20"): 4, ("09:20", "12:00"): 5,
           ("09:45", "12:00"): 6}  # fmt: skip
STOP_TIER = 7
PROBES = (  # (underlying, opt, rel, day, compare with the app's store)
    ("NIFTY", "PE", 0, dt.date(2024, 7, 23), True),
    ("BANKNIFTY", "PE", 4, dt.date(2021, 11, 26), True),
    ("NIFTY", "PE", 0, dt.date(2021, 11, 8), False),
)


# ───────────────────────────── the ledger ───────────────────────────────────


class Ledger:
    """Every request sent and every need, in its own SQLite file (never the bar store)."""

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, at TEXT NOT NULL,"
            " underlying TEXT, code INTEGER, opt TEXT, rel INTEGER, day TEXT, ok INTEGER,"
            " bars INTEGER, err TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS needs (tier INTEGER NOT NULL, underlying TEXT NOT NULL,"
            " code INTEGER NOT NULL, opt TEXT NOT NULL, rel INTEGER NOT NULL, day TEXT NOT NULL,"
            " why TEXT, PRIMARY KEY (underlying, code, opt, rel, day))"
        )
        self.db.commit()

    def log_request(self, key: RollingKey, day: dt.date, ok: bool, bars: int, err: str,
                    at: dt.datetime) -> None:  # fmt: skip
        self.db.execute(
            "INSERT INTO requests (at, underlying, code, opt, rel, day, ok, bars, err)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (at.isoformat(timespec="seconds"), key.underlying, key.code, key.opt, key.rel,
             day.isoformat(), int(ok), int(bars), err),
        )  # fmt: skip
        self.db.commit()

    def count_requests(self, before: dt.datetime | None = None) -> int:
        if before is None:
            return int(self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        q = "SELECT COUNT(*) FROM requests WHERE at < ?"
        return int(self.db.execute(q, (before.isoformat(timespec="seconds"),)).fetchone()[0])

    def failures(self, key: RollingKey, day: dt.date) -> int:
        q = ("SELECT COUNT(*) FROM requests WHERE ok = 0 AND underlying=? AND code=? AND opt=?"
             " AND rel=? AND day=?")  # fmt: skip
        args = (key.underlying, key.code, key.opt, key.rel, day.isoformat())
        return int(self.db.execute(q, args).fetchone()[0])

    def add_needs(self, rows: Iterable[tuple[int, RollingKey, dt.date, str]]) -> int:
        """Insert needs; a need listed twice keeps its lower tier. Returns rows inserted or
        moved to a lower tier."""
        before = self.db.total_changes
        self.db.executemany(
            "INSERT INTO needs VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET"
            " tier = excluded.tier, why = excluded.why WHERE excluded.tier < needs.tier",
            [(t, k.underlying, k.code, k.opt, k.rel, d.isoformat(), why) for t, k, d, why in rows],
        )
        self.db.commit()
        return self.db.total_changes - before

    def needs(self, max_tier: int | None = None) -> list[tuple[int, RollingKey, dt.date]]:
        """In fetch order: tier, then series, then day (so one request covers two days)."""
        q = "SELECT tier, underlying, code, opt, rel, day FROM needs"
        args: tuple = ()
        if max_tier is not None:
            q += " WHERE tier <= ?"
            args = (max_tier,)
        q += " ORDER BY tier, underlying, code, opt, rel, day"
        return [(int(t), RollingKey(u, int(c), o, int(r)), dt.date.fromisoformat(d))
                for t, u, c, o, r, d in self.db.execute(q, args)]  # fmt: skip


# ───────────────────────────── what is already held ─────────────────────────


class ReadOnlyBlocks:
    """The `blocks` table of an option store opened with SQLite `mode=ro` (never written)."""

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)

    def complete(self, key: RollingKey, day: dt.date) -> bool:
        row = self.db.execute(
            "SELECT complete FROM blocks WHERE underlying=? AND flag=? AND code=? AND opt=?"
            " AND rel=? AND day=?",
            (key.underlying, key.flag, key.code, key.opt, key.rel, day.isoformat()),
        ).fetchone()
        return bool(row and row[0])

    def data(self, key: RollingKey, day: dt.date) -> dict[str, np.ndarray] | None:
        row = self.db.execute(
            "SELECT data FROM blocks WHERE underlying=? AND flag=? AND code=? AND opt=?"
            " AND rel=? AND day=? AND rows > 0",
            (key.underlying, key.flag, key.code, key.opt, key.rel, day.isoformat()),
        ).fetchone()
        return _unpack(row[0]) if row else None

    def days(self, und: str, opt: str, rel: int, nonempty: bool = True) -> set[dt.date]:
        q = ("SELECT day FROM blocks WHERE underlying=? AND flag='MONTH' AND code=1 AND opt=?"
             " AND rel=?" + (" AND rows > 0" if nonempty else ""))  # fmt: skip
        return {dt.date.fromisoformat(d) for (d,) in self.db.execute(q, (und, opt, rel))}


def held_by(store: OptionBarStore, others: list[ReadOnlyBlocks]) -> Callable[..., bool]:
    """A need is held when our store or any other store has its day, complete."""

    def known(key: RollingKey, day: dt.date) -> bool:
        blk = store.get(key, day)
        if blk is not None and blk.complete:
            return True
        return any(o.complete(key, day) for o in others)

    return known


# ───────────────────────────── the fetch loop ───────────────────────────────


def run_needs(
    source: Any,
    store: OptionBarStore,
    unds: dict[str, Underlying],
    needs: Iterable[tuple[int, RollingKey, dt.date]],
    ledger: Ledger,
    known: Callable[[RollingKey, dt.date], bool],
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(IST),
    mono: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline: dt.datetime | None = None,
    cap: int | None = None,
    min_gap: float = MIN_GAP_S,
    max_fails: int = MAX_FAILS_IN_A_ROW,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], str]:
    """Fetch every need not held, in the order given; one request stores two days.

    At most one request every `min_gap` seconds; stops at `deadline`, after `cap` requests or
    after `max_fails` refusals in a row. Every request is logged in the ledger before the next
    one. Returns the counts and why it stopped ("done", "deadline", "cap", "refused")."""
    counts = {"requests": 0, "ok": 0, "failed": 0, "held": 0, "given_up": 0, "empty_days": 0}
    in_a_row = 0
    last: float | None = None
    for _tier, key, day in needs:
        if known(key, day):
            counts["held"] += 1
            continue
        if ledger.failures(key, day) >= MAX_TRIES:
            counts["given_up"] += 1
            continue
        if cap is not None and counts["requests"] >= cap:
            return counts, "cap"
        if deadline is not None and now() >= deadline:
            return counts, "deadline"
        if last is not None:
            wait = min_gap - (mono() - last)
            if wait > 0:
                sleep(wait)
        last = mono()
        counts["requests"] += 1
        at = now()
        try:
            blocks = fetch_days(source, unds[key.underlying], key, day, at.date())
        except Exception as exc:  # noqa: BLE001 - logged, counted, three in a row stop it
            counts["failed"] += 1
            in_a_row += 1
            ledger.log_request(key, day, False, 0, f"{type(exc).__name__}: {str(exc)[:160]}", at)
            if progress:
                progress(f"{key.underlying} {key.opt} {key.strike_arg} {day} FAILED "
                         f"({type(exc).__name__})")  # fmt: skip
            if in_a_row >= max_fails:
                return counts, "refused"
            continue
        in_a_row = 0
        counts["ok"] += 1
        for b in blocks:
            store.put(b)
            counts["empty_days"] += int(b.rows == 0)
        ledger.log_request(key, day, True, blocks[0].rows if blocks else 0, "", at)
        if progress and counts["requests"] % 50 == 0:
            progress(f"{counts['requests']} requests, last {key.underlying} {key.opt} "
                     f"{key.strike_arg} {day}")  # fmt: skip
    return counts, "done"


# ───────────────────────────── the plan ─────────────────────────────────────


def weekdays(first: dt.date = FIRST, last: dt.date = LAST) -> list[dt.date]:
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)
            if (first + dt.timedelta(days=i)).weekday() < 5]  # fmt: skip


def ist_minutes(ts: np.ndarray) -> np.ndarray:
    return ((np.asarray(ts, dtype=np.int64) + 19800) % 86400) // 60


def atm_at(bars: dict[str, np.ndarray] | None, minute: int) -> float | None:
    """Dhan's at-the-money strike of the first rel-0 bar within 3 minutes of `minute`."""
    if bars is None or not len(bars["ts"]):
        return None
    mi = ist_minutes(bars["ts"])
    k = np.flatnonzero((mi >= minute) & (mi <= minute + MAX_WAIT))
    return float(bars["strike"][k[0]]) if len(k) else None


def exit_rel(k_entry: float, atm_exit: float, step: float) -> int | None:
    """The rolling offset that holds strike `k_entry` when the money is at `atm_exit`."""
    r = (k_entry - atm_exit) / step
    if abs(r - round(r)) > 1e-6:
        return None
    return int(round(r))


def pair_needs(und: str, day: dt.date, atm: Callable[[int], float | None], step: float,
               ) -> list[tuple[int, RollingKey, dt.date, str]]:  # fmt: skip
    """Tiers 3 to 6 of one session: the CALL and PUT at the bought strike at each exit."""
    out = []
    for (e, x), tier in TIER_OF.items():
        k, a = atm(ENTRIES[e]), atm(EXITS[x])
        if k is None or a is None:
            continue
        r = exit_rel(k, a, step)
        if r is None or r == 0 or abs(r) > 10:
            continue
        for opt in ("CE", "PE"):
            out.append((tier, RollingKey(und, 1, opt, r), day, f"{e} bought, {x} sold"))
    return out


def plan(ledger: Ledger, mine: ReadOnlyBlocks, app: ReadOnlyBlocks,
         unds: dict[str, Underlying]) -> dict[int, int]:  # fmt: skip
    """Write every need the data allows now; returns the needs added per tier."""
    added: dict[int, int] = {}

    def add(tier: int, rows: list) -> None:
        added[tier] = added.get(tier, 0) + ledger.add_needs(rows)

    for und in SERIES:
        add(1, [(1, RollingKey(und, 1, "PE", 0), d, "atm put") for d in weekdays()])
        traded = mine.days(und, "PE", 0)
        have_call = app.days(und, "CE", 0, nonempty=False) | mine.days(und, "CE", 0, False)
        add(2, [(2, RollingKey(und, 1, "CE", 0), d, "atm call the store lacks")
                for d in sorted(traded - have_call) if FIRST <= d <= LAST])  # fmt: skip
        step = unds[und].strike_step
        rows: list = []
        days = sorted(traded | app.days(und, "CE", 0) | mine.days(und, "CE", 0))
        for d in days:
            if not FIRST <= d <= LAST:
                continue
            blocks = [b for b in (app.data(RollingKey(und, 1, "CE", 0), d),
                                  mine.data(RollingKey(und, 1, "CE", 0), d),
                                  mine.data(RollingKey(und, 1, "PE", 0), d)) if b is not None]  # fmt: skip

            def atm(minute: int, blocks: list = blocks) -> float | None:
                for b in blocks:
                    v = atm_at(b, minute)
                    if v is not None:
                        return v
                return None

            rows.extend(pair_needs(und, d, atm, step))
        for tier in sorted(TIER_OF.values()):
            add(tier, [r for r in rows if r[0] == tier])
    return added


def stop_needs(path: Path) -> list[tuple[int, RollingKey, dt.date, str]]:
    with open(path, newline="") as f:
        return [(STOP_TIER, RollingKey(r["underlying"], 1, r["opt"], int(r["rel"])),
                 dt.date.fromisoformat(r["day"]), "stop rule sold") for r in csv.DictReader(f)]  # fmt: skip


# ───────────────────────────── stages ───────────────────────────────────────


def paths() -> tuple[Path, Path, Path, Path]:
    root = Path.cwd().resolve()
    app_store = root / "data" / "expired_options.db"
    if not app_store.is_file() or root == Path(__file__).resolve().parents[1]:
        sys.exit("run from the main checkout (it holds data/expired_options.db)")
    out = root / STUDY
    out.mkdir(parents=True, exist_ok=True)
    return root, app_store, out / "options.sqlite", out / "ledger.sqlite"


def build_source(rate: float) -> tuple[Any, dict[str, Underlying]]:
    """The app's own adapter and universe, built the way `expired_options_fetch` builds them.
    The engine's dashboard secret is not needed here: a placeholder keeps `Secrets.load` from
    reading or creating `data/api_token.txt`."""
    os.environ.setdefault("ONELOT_API_TOKEN", "research-fetch-placeholder-" + "x" * 24)
    from expired_options_fetch import research_adapter, underlyings

    unds = underlyings()
    return research_adapter(rate), {k: unds[k] for k in SERIES}


def gate_today(t: dt.time) -> dt.datetime:
    return dt.datetime.combine(dt.datetime.now(IST).date(), t, tzinfo=IST)


def stage_probe() -> None:
    _, app_path, store_path, ledger_path = paths()
    ledger = Ledger(ledger_path)
    now = dt.datetime.now(IST)
    sent = ledger.count_requests()
    if now < gate_today(OPEN_GATE) and sent + len(PROBES) > MAX_BEFORE_GATE:
        sys.exit(f"{sent} requests already sent: the probe would exceed {MAX_BEFORE_GATE}")
    source, unds = build_source(1.0)
    store, app = OptionBarStore(store_path), ReadOnlyBlocks(app_path)
    for und, opt, rel, day, compare in PROBES:
        key = RollingKey(und, 1, opt, rel)
        time.sleep(MIN_GAP_S)
        at = dt.datetime.now(IST)
        try:
            blocks = fetch_days(source, unds[und], key, day, at.date())
        except Exception as exc:  # noqa: BLE001
            ledger.log_request(key, day, False, 0, f"{type(exc).__name__}: {str(exc)[:160]}", at)
            print(f"{und} {opt} {key.strike_arg} {day}: FAILED {type(exc).__name__}")
            continue
        ledger.log_request(key, day, True, blocks[0].rows if blocks else 0, "", at)
        for b in blocks:
            store.put(b)
            line = f"{und} {opt} {key.strike_arg} {b.day}: {b.rows} bars"
            if b.rows:
                mi = ist_minutes(b.data["ts"])
                k920 = np.flatnonzero(mi == 560)
                if len(k920):
                    j = int(k920[0])
                    line += (f", 09:20 strike {b.data['strike'][j]:.0f} spot "
                             f"{b.data['spot'][j]:.1f} open {b.data['open'][j]:.2f}")  # fmt: skip
            if compare:
                ref = app.data(key, b.day)
                line += "; app store: " + same_bars(b, ref)
            print(line, flush=True)
    print(f"requests in the ledger: {ledger.count_requests()}")


def same_bars(b: Block, ref: dict[str, np.ndarray] | None) -> str:
    if ref is None:
        return "no block"
    if b.rows != len(ref["ts"]) or not np.array_equal(b.data["ts"], ref["ts"]):
        return f"DIFFERENT minutes ({b.rows} against {len(ref['ts'])})"
    worst = max(float(np.nanmax(np.abs(np.round(b.data[c], 2) - ref[c])))
                for c in ("open", "high", "low", "close", "strike"))  # fmt: skip
    return f"same {b.rows} minutes, largest price or strike difference {worst:.2f}"


def stage_plan(stops: str | None) -> None:
    _, app_path, store_path, ledger_path = paths()
    ledger = Ledger(ledger_path)
    OptionBarStore(store_path).close()  # the schema exists before it is read
    mine, app = ReadOnlyBlocks(store_path), ReadOnlyBlocks(app_path)
    from expired_options_fetch import underlyings

    unds = {k: v for k, v in underlyings().items() if k in SERIES}
    if stops:
        print("tier 7 added:", ledger.add_needs(stop_needs(Path(stops))))
        return
    print("needs added by tier:", plan(ledger, mine, app, unds))


def stage_fetch(max_tier: int | None) -> None:
    _, app_path, store_path, ledger_path = paths()
    now = dt.datetime.now(IST)
    if now < gate_today(OPEN_GATE):
        sys.exit(f"the bulk fetch waits for {OPEN_GATE:%H:%M} IST (now {now:%H:%M})")
    ledger = Ledger(ledger_path)
    source, unds = build_source(1.0)
    store = OptionBarStore(store_path)
    known = held_by(store, [ReadOnlyBlocks(app_path)])
    needs = ledger.needs(max_tier)
    print(f"{now:%H:%M:%S} needs listed {len(needs)}, requests so far "
          f"{ledger.count_requests()}", flush=True)  # fmt: skip

    def say(msg: str) -> None:
        print(f"{dt.datetime.now(IST):%H:%M:%S} {msg}", flush=True)

    counts, why = run_needs(source, store, unds, needs, ledger, known,
                            deadline=gate_today(DEADLINE), progress=say)  # fmt: skip
    say(f"stopped: {why}; {counts}; requests in the ledger {ledger.count_requests()}")


def stage_status() -> None:
    _, app_path, store_path, ledger_path = paths()
    ledger = Ledger(ledger_path)
    known = held_by(OptionBarStore(store_path), [ReadOnlyBlocks(app_path)])
    left: dict[int, int] = {}
    total: dict[int, int] = {}
    for tier, key, day in ledger.needs():
        total[tier] = total.get(tier, 0) + 1
        if not known(key, day):
            left[tier] = left.get(tier, 0) + 1
    q = "SELECT ok, COUNT(*) FROM requests GROUP BY ok"
    print("requests (ok flag, count):", ledger.db.execute(q).fetchall())
    print("needs per tier:", total, "not held:", left)
    mine = sqlite3.connect(f"{store_path.resolve().as_uri()}?mode=ro", uri=True)
    rows = mine.execute(
        "SELECT underlying, opt, COUNT(*), SUM(rows > 0), SUM(rows) FROM blocks"
        " GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    for r in rows:
        print("  blocks", r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("probe", "plan", "fetch", "status"))
    ap.add_argument("--stops", help="plan: a CSV of stop-rule exits (tier 7)")
    ap.add_argument("--max-tier", type=int)
    a = ap.parse_args()
    if a.stage == "probe":
        stage_probe()
    elif a.stage == "plan":
        stage_plan(a.stops)
    elif a.stage == "fetch":
        stage_fetch(a.max_tier)
    else:
        stage_status()


if __name__ == "__main__":
    main()
