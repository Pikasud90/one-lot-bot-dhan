"""Desk-to-Lab parity replay for the multi-timeframe Supertrend slot (2026-11-11).

Feeds stored one-minute bars, one minute at a time, through the desk's own tracker
(`desk/tracker.InstrumentTracker`) and the paper book's own rules (`paper/fill.decide_fill`,
`paper/manage.manage_step`), the way a running desk sees them, and lists the trades. The
report sets them beside the Lab's trades of the same settings
(`data/research/mtf_2026-11-11/lab/runs_INDEX_OPTIONS.pkl`, from `scripts/mtf_lab_check.py`).

    .venv/bin/python scripts/mtf_desk_replay.py --run [--workers 5]
    .venv/bin/python scripts/mtf_desk_replay.py --report

Two timings of each setting are replayed:
  lab   the alert is stamped a second before its bar closes, so the trade fills at the open of
        the very next minute: the Lab's fill. Entries, stops and exits should then match the
        Lab's trade for trade.
  desk  the alert is stamped at the poll, five seconds after the bar closes, so the trade
        fills one minute later: what the running desk does.

What it leaves out: `Desk.poll` itself (it reads the wall clock in many places), the option
chain and premiums (levels here are the index's, which is what the stop and the trail act
on), and the one-live-idea-per-instrument rule (each setting keeps its own book here, as each
Lab run does). Each day starts with the 25 days of bars a freshly started desk loads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "data" / "research" / "mtf_2026-11-11"
STORE = OUT / "replay"
LAB = OUT / "lab" / "runs_INDEX_OPTIONS.pkl"
DEST = OUT / "mtf_desk_replay.txt"

WINDOWS = (("2026-08-03", "2026-08-25"), ("2026-10-27", "2026-11-06"))
HISTORY_DAYS = 25  # config/desk.yaml history_days
INDICES = {
    "BANKNIFTY": "BANKNIFTY_OPT",
    "NIFTY": "NIFTY_OPT",
    "FINNIFTY": "FINNIFTY_OPT",
    "MIDCPNIFTY": "MIDCPNIFTY_OPT",
    "SENSEX": "SENSEX_OPT",
}
_BASE = {
    "allow_long": True, "allow_short": False, "st_period": 14, "st_mult": 1.7,
    "gate": "all", "trend_tf": "30m",
}  # fmt: skip
#: setting -> (the slot's params, the Lab variant it must match)
SETTINGS: dict[str, tuple[dict, str]] = {
    "slot": (
        {
            **_BASE, "flip_window": 4, "trend_adx_min": 25, "trend_rsi_min": 60,
            "entry_end": "13:00", "trail_tf": "60m", "stop_buffer_atr": 2.0,
            "max_trades_per_day": 1,
        },
        "fresh turn + ADX/RSI, 60m line, close + 2 ATR",
    ),
    "owner": (
        {**_BASE, "reenter_daily": True, "entry_end": "14:45", "stop_buffer_atr": 0.0},
        "owner rule, 30m line, touch",
    ),
}  # fmt: skip
TIMINGS = ("lab", "desk")
LAB_REASON = {
    "STOP": "stop", "TRAIL_STOP": "trail", "SIGNAL_EXIT": "signal", "OPPOSITE_SIGNAL": "signal",
    "SESSION_END": "square_off", "TARGET": "target", "TIME_STOP": "time",
}  # fmt: skip


@dataclass
class Book:
    """One setting's own book: at most one pending alert or one open position."""

    setting: str
    timing: str
    pending: dict | None = None
    state: object | None = None
    open_row: dict | None = None
    rows: list[dict] = field(default_factory=list)


def _replay(
    task: tuple[str, str, tuple[str, str]],
) -> tuple[str, tuple[str, str], list[dict], float]:
    from onelot.clock import IST
    from onelot.data.db import get_engine
    from onelot.data.store import _read_bars
    from onelot.data.universe import load_specs
    from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg
    from onelot.desk.history import daily_history
    from onelot.desk.tracker import InstrumentTracker
    from onelot.paper.fill import decide_fill, normalise_bars
    from onelot.paper.manage import STOP_MOVED, StrategyExit, TrailLevel, manage_step
    from onelot.paper.money import to_paise
    from onelot.paper.position import initial_position, rules_from_alert

    key, tracked, window = task
    t0 = time.time()
    engine = get_engine()
    series, instruments = load_specs()
    first = pd.Timestamp(window[0], tz=IST)
    last = pd.Timestamp(window[1], tz=IST) + pd.Timedelta(days=1)
    bars = _read_bars(
        engine, key, "1m", start=first - pd.Timedelta(days=HISTORY_DAYS + 1), end=last
    )
    slots_cfg, books = [], []
    for setting, (params, _) in SETTINGS.items():
        for timing in TIMINGS:
            slots_cfg.append(StrategySlotCfg("supertrend_mtf", "5m", dict(params)))
            books.append(Book(setting, timing))
    tracker = InstrumentTracker(
        DeskInstrumentCfg(key, tracked, "INDEX", tuple(slots_cfg)),
        series[key],
        instruments[tracked],
        1,
    )
    tick_paise = to_paise(instruments[tracked].tick_size)
    index = bars.index
    start_i = int(index.searchsorted(first))
    day: dt.date | None = None
    lo_i = day_i = 0
    trade_id = 0
    for i in range(start_i, len(index)):
        ts = index[i]
        if ts.date() != day:
            day = ts.date()
            day_i = i
            lo_i = int(index.searchsorted(ts.normalize() - pd.Timedelta(days=HISTORY_DAYS)))
            morning = dt.datetime.combine(day, dt.time(9, 0), tzinfo=IST)
            tracker.daily_history = daily_history(engine, key, morning)
            tracker.history_day = day
            for slot in tracker.slots:
                slot.signals_today = 0
        tracker.bars = bars.iloc[lo_i : i + 1]  # not update_bars: its cutoff is the wall clock
        close_at = ts + pd.Timedelta(minutes=1)
        now = (close_at + pd.Timedelta(seconds=5)).to_pydatetime()
        fired = {id(slot): v for slot, v in tracker.evaluate(now, None, True)}
        today = bars.iloc[day_i : i + 1]
        for slot, book in zip(tracker.slots, books, strict=True):
            if slot.error:
                raise RuntimeError(f"{key} {book.setting}: {slot.error}")
            # 1) a pending alert fills at the open of the first bar after it (paper/fill.py)
            if book.pending is not None:
                p = book.pending
                dec = decide_fill(
                    normalise_bars(today),
                    created_at=p["created_at"],
                    deadline=p["created_at"] + dt.timedelta(minutes=10),
                    resolution_min=1,
                    now=now,
                    direction=p["side"],
                    stop_paise=p["stop_paise"],
                    exit_time=dt.time(15, 15),
                )
                if dec.outcome == "fill":
                    trade_id += 1
                    book.state = initial_position(
                        trade_id=trade_id,
                        direction=p["side"],
                        kind="INDEX",
                        qty=1,
                        entry_paise=dec.bar_open_paise,
                        levels={"underlying_stop_paise": p["stop_paise"]},
                        opened_at=dec.bar_ts,
                        option=None,
                    )
                    book.open_row = {
                        **p["row"],
                        "fill_ts": dec.bar_ts,
                        "entry": dec.bar_open_paise / 100,
                        "moves": [],
                    }
                    book.pending = None
                elif dec.outcome == "no_fill":
                    book.rows.append({**p["row"], "no_fill": dec.reason})
                    book.pending = None
            # 2) the open position, by the paper book's rules (paper/manage.py)
            if book.state is not None:
                side = book.state.direction
                closed_at = slot.last_bar_ts + dt.timedelta(minutes=slot.tf_min)
                advice = slot.exit_advice(side)
                level = slot.trail_level(side)
                new, events = manage_step(
                    book.state,
                    today,
                    book.open_row["rules"],
                    now,
                    StrategyExit(advice, closed_at) if advice else None,
                    TrailLevel(to_paise(level), closed_at) if level is not None else None,
                )
                for ev in events:
                    if ev.kind == STOP_MOVED:
                        book.open_row["moves"].append((ev.ts, ev.detail["to_paise"] / 100))
                if new.closed is not None:
                    row = book.open_row
                    row.pop("rules")
                    row.update(
                        exit_ts=new.closed.at,
                        exit_reason=new.closed.reason,
                        exit=None
                        if new.closed.price_paise is None
                        else new.closed.price_paise / 100,
                        final_stop=new.stop_paise / 100,
                    )
                    book.rows.append(row)
                    slot.last_exit_at = new.closed.at
                    book.state, book.open_row = None, None
                else:
                    book.state = new
            # 3) a fresh entry becomes an alert (Desk._raise_signal), one live idea per book
            v = fired.get(id(slot))
            if v is not None and book.state is None and book.pending is None:
                manage = {
                    "target_r": float(slot.strategy.p.target_r or 0),
                    "trail_atr_mult": float(slot.strategy.p.trail_atr_mult or 0),
                    "trail_activate_r": float(slot.strategy.p.trail_activate_r or 1.0),
                    "max_bars": int(slot.strategy.p.max_bars or 0),
                    "exit_time": "15:15",
                    "atr": slot.last_features.get("atr"),
                    "atr_trail": slot.last_features.get("atr_trail"),
                    "tf_min": slot.tf_min,
                    "horizon": "intraday",
                }
                if slot.line_trail:
                    manage["line_trail"] = True
                rules = rules_from_alert(
                    {"manage": manage}, tick_paise=tick_paise, resolution=1, timeframe="5m"
                )
                created = (
                    now
                    if book.timing == "desk"
                    else (close_at - pd.Timedelta(seconds=1)).to_pydatetime()
                )
                book.pending = {
                    "created_at": created,
                    "side": v.direction,
                    "stop_paise": to_paise(v.stop),
                    "row": {
                        "instrument": tracked,
                        "setting": book.setting,
                        "timing": book.timing,
                        "signal_bar": v.bar_ts,
                        "alert_at": created,
                        "side": v.direction,
                        "stop": v.stop,
                        "close": v.close,
                        "line_trail": bool(slot.line_trail),
                        "rules": rules,
                    },
                }
                slot.signals_today += 1
    rows = [r for b in books for r in b.rows]
    for b in books:  # anything still open at the end of the window is listed as such
        if b.open_row is not None:
            b.open_row.pop("rules")
            rows.append({**b.open_row, "exit_reason": "OPEN"})
        if b.pending is not None:
            rows.append({**{k: v for k, v in b.pending["row"].items() if k != "rules"},
                         "no_fill": "window ended"})  # fmt: skip
    for r in rows:
        r.pop("rules", None)
    return tracked, window, rows, time.time() - t0


def run(workers: int) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tasks = [(key, tracked, w) for key, tracked in INDICES.items() for w in WINDOWS]
    t0 = time.time()
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_replay, t) for t in tasks]
        for n, fut in enumerate(as_completed(futs), 1):
            tracked, window, out, secs = fut.result()
            rows += out
            print(
                f"[{n}/{len(tasks)}] {tracked} {window[0]}..{window[1]}: {len(out)} rows in "
                f"{secs:.0f}s (total {time.time() - t0:.0f}s)",
                flush=True,
            )
    with (STORE / "replay.pkl").open("wb") as fh:
        pickle.dump(rows, fh)
    print("saved", flush=True)


def _lab_trades(per_inst: dict, variant: str) -> pd.DataFrame:
    """The Lab's trades of `variant` inside the replay windows, all instruments."""
    frames = []
    for inst, runs in per_inst.items():
        tr = runs.get(variant)
        if not isinstance(tr, pd.DataFrame) or tr.empty:
            continue
        tr = tr.copy()
        tr["instrument"] = inst
        tr["entry_ts"] = pd.to_datetime(tr["entry_ts"]).dt.tz_convert("Asia/Kolkata")
        tr["exit_ts"] = pd.to_datetime(tr["exit_ts"], utc=True).dt.tz_convert("Asia/Kolkata")
        frames.append(tr)
    if not frames:
        return pd.DataFrame()
    lab = pd.concat(frames, ignore_index=True)
    keep = pd.Series(False, index=lab.index)
    for a, b in WINDOWS:
        lo = pd.Timestamp(a, tz="Asia/Kolkata")
        hi = pd.Timestamp(b, tz="Asia/Kolkata") + pd.Timedelta(days=1)
        keep |= (lab["entry_ts"] >= lo) & (lab["entry_ts"] < hi)
    return lab[keep].sort_values(["instrument", "entry_ts"]).reset_index(drop=True)


def report() -> None:
    with (STORE / "replay.pkl").open("rb") as fh:
        rows = pickle.load(fh)  # noqa: S301 - our own file
    with LAB.open("rb") as fh:
        per_inst = pickle.load(fh)  # noqa: S301 - our own file
    lines = [
        "DESK REPLAY BESIDE THE LAB: supertrend_mtf through the tracker and the paper rules",
        "=" * 100,
        "Stored one-minute bars of the five indices, fed minute by minute through",
        "desk/tracker.InstrumentTracker (5m slots, 25 days of bars at each day's start, the",
        "stored daily history) and paper/fill.decide_fill + paper/manage.manage_step.",
        f"Windows: {', '.join(f'{a}..{b}' for a, b in WINDOWS)}.",
        "A trade MATCHES when the replay fills in the Lab's entry bar (lab timing: the same",
        "minute; desk timing: one minute later), ends in the Lab's exit bar (the Lab stamps the",
        "5-minute bar, the paper book the minute) and for the same reason.",
        "",
    ]
    frame = pd.DataFrame(rows)
    total_bad = 0
    for setting, (_, variant) in SETTINGS.items():
        lab = _lab_trades(per_inst, variant)
        for timing in TIMINGS:
            mine = frame[(frame["setting"] == setting) & (frame["timing"] == timing)]
            filled = mine[mine["fill_ts"].notna()] if "fill_ts" in mine else mine.iloc[0:0]
            late = pd.Timedelta(minutes=0 if timing == "lab" else 1)
            used: set[int] = set()
            ok = entry_only = 0
            notes: list[str] = []
            for _, lt in lab.iterrows():
                cand = filled[
                    (filled["instrument"] == lt["instrument"])
                    & (filled["fill_ts"] == lt["entry_ts"] + late)
                ]
                if cand.empty:
                    notes.append(
                        f"  LAB ONLY   {lt['instrument']:15s} {lt['entry_ts']:%Y-%m-%d %H:%M} -> "
                        f"{lt['exit_ts']:%H:%M} {lt['exit_reason']}"
                    )
                    continue
                r = cand.iloc[0]
                used.add(int(cand.index[0]))
                same_bar = pd.Timestamp(r["exit_ts"]).floor("5min") == lt["exit_ts"]
                same_why = LAB_REASON.get(r["exit_reason"]) == lt["exit_reason"]
                if same_bar and same_why:
                    ok += 1
                else:
                    entry_only += 1
                    notes.append(
                        f"  EXIT DIFF  {lt['instrument']:15s} {lt['entry_ts']:%Y-%m-%d %H:%M}: "
                        f"Lab {lt['exit_ts']:%H:%M} {lt['exit_reason']}, replay "
                        f"{pd.Timestamp(r['exit_ts']):%H:%M} {r['exit_reason']} "
                        f"(stop {r['stop']:.2f} -> {r['final_stop']:.2f}, {len(r['moves'])} moves)"
                    )
            for ix, r in filled.iterrows():
                if int(ix) not in used:
                    notes.append(
                        f"  REPLAY ONLY {r['instrument']:15s} {pd.Timestamp(r['fill_ts']):%Y-%m-%d %H:%M}"
                        f" -> {pd.Timestamp(r['exit_ts']):%H:%M} {r['exit_reason']}"
                    )
            unfilled = mine[mine["fill_ts"].isna()] if "fill_ts" in mine else mine
            for _, r in unfilled.iterrows():
                notes.append(
                    f"  NO FILL    {r['instrument']:15s} alert {pd.Timestamp(r['alert_at']):%Y-%m-%d %H:%M}"
                    f": {r.get('no_fill')}"
                )
            moves = int(filled["moves"].map(len).sum()) if len(filled) else 0
            reasons = filled["exit_reason"].value_counts().to_dict() if len(filled) else {}
            bad = len(lab) - ok + sum(1 for n in notes if n.startswith("  REPLAY ONLY"))
            total_bad += bad if timing == "lab" else 0
            lines += [
                f"{setting.upper()} / {timing} timing   (Lab variant: {variant})",
                f"  Lab trades {len(lab)}, replay trades {len(filled)}, matched {ok}, "
                f"same entry but another exit {entry_only}",
                f"  replay exits {reasons}; stop moves along the line {moves}",
                *notes,
                "",
            ]
    lines.append(
        "VERDICT (lab timing): "
        + (
            "every trade matches the Lab."
            if total_bad == 0
            else f"{total_bad} difference(s), listed above."
        )
    )
    DEST.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()
    if a.run:
        run(a.workers)
    if a.report:
        report()


if __name__ == "__main__":
    main()
