"""US-079: replay the micro-trend slot's paper trades over stored sessions through the REAL
paper book, and check every exit against a direct loop over `manage/ratchet.py`.
READ-ONLY on the store.

    PYTHONPATH=src .venv/bin/python scripts/ratchet_desk_replay.py --store PATH/onelot.db \\
        --sessions 20 [--instrument NIFTY]

The store is opened through an SQLite URI with mode=ro, never through the app's engine or
settings. The micro-trend slot of `config/desk.yaml` (its settings, `ratchet: true` included)
gives the signals on the stored one-minute bars, built as the Simulator builds them
(`sim/engine.build_signals`, 120 days of history before the window). One live idea at a
time, as on the desk: a signal while a trade is open is dropped. Each signal becomes a
desk-shaped alert one second after its 3m bar closes, with the first risk the desk reads
(`manage/live.first_risk`), and opens a paper trade in a `PaperBook` on a temporary
database; the book is stepped once after the session with that session's bars and the
strategy's exit (the first 3m close after the fill where 3 of 7 turn against).

The reference is a plain loop: the fill at the first one-minute open after the alert, then
per bar the strategy's exit or the session end at the open (a gap through a level outranks
them), else `ratchet.exit_on_bar`, else `ratchet.advance`. Every trade's exit time, level
and reason are compared; the option's premium is a straight line in the index (its delta),
which prices the paper trade's money but does not touch the index levels compared here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from onelot.clock import IST  # noqa: E402
from onelot.config import BacktestCfg, CostConfig, PaperCfg  # noqa: E402
from onelot.costs import CostModel  # noqa: E402
from onelot.data.universe import load_specs  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402
from onelot.manage import live as rlive  # noqa: E402
from onelot.manage import ratchet  # noqa: E402

STRATEGY, TIMEFRAME = "microtrend", "3m"
HISTORY_DAYS = 120
DELTA = 0.65
EXIT_TIME = dt.time(15, 15)


def read_only_engine(path: Path):  # noqa: ANN201
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


@dataclass
class Signal:
    side: int
    close_at: pd.Timestamp  # the 3m signal bar's close
    price: float  # its close: the alert's price
    exit_at: pd.Timestamp | None  # the first strategy exit's bar close after it, if any


def slot_of(instrument: str) -> tuple[str, dict[str, Any]]:
    """(the instrument key the alert is in, the slot's settings) from config/desk.yaml."""
    cfg = load_desk_config()
    for inst in cfg.indices:
        if inst.key == instrument:
            for s in inst.strategies:
                if s.name == STRATEGY and s.timeframe == TIMEFRAME:
                    return inst.tracked, {k: v for k, v in s.params.items() if k != "evidence"}
    raise SystemExit(f"no {STRATEGY} {TIMEFRAME} slot for {instrument} in config/desk.yaml")


def signals(store: Path, instrument: str, sessions: int) -> tuple[list[Signal], pd.DataFrame]:
    """The slot's signals in the last `sessions` stored sessions, and the one-minute bars."""
    from onelot.sim.bars import StoreBars
    from onelot.sim.engine import build_signals
    from onelot.strategies.registry import get_strategy

    series, instruments = load_specs()
    tracked, params = slot_of(instrument)
    spec = instruments[tracked]
    bars = StoreBars(read_only_engine(store))
    minute = bars.minute_bars(instrument, None, None)
    if minute.empty:
        raise SystemExit(f"no one-minute bars of {instrument} in {store}")
    days = sorted(set(pd.DatetimeIndex(minute.index).date))
    window = days[-sessions:]
    lo = pd.Timestamp(window[0] - dt.timedelta(days=HISTORY_DAYS)).tz_localize(IST)
    minute = minute[minute.index >= lo]
    strategy = get_strategy(STRATEGY, {**params, "timeframe": TIMEFRAME})
    f, _ = build_signals(
        strategy, spec, instrument, TIMEFRAME, minute, bars, series.get(instrument)
    )
    if f is None:
        return [], minute
    width = pd.Timedelta(minutes=3)
    out: list[Signal] = []
    for k in range(len(f)):
        at = pd.Timestamp(f.index[k])
        if at.date() < window[0]:
            continue
        le, se = bool(f["long_entry"].iloc[k]), bool(f["short_entry"].iloc[k])
        if not (le or se):
            continue
        side = 1 if le else -1
        close_at = min(at + width, at.normalize() + pd.Timedelta(hours=15, minutes=30))
        if close_at.time() >= EXIT_TIME:
            continue
        col = "long_exit" if side == 1 else "short_exit"
        later = f.index[(f.index > at) & f[col].to_numpy(bool)]
        later = later[pd.DatetimeIndex(later).date == at.date()]
        exit_at = (pd.Timestamp(later[0]) + width) if len(later) else None
        out.append(Signal(side, close_at, float(f["close"].iloc[k]), exit_at))
    return out, minute


def alert_for(sig: Signal, instrument: str, spec: Any, risk: float, cost: float) -> dict:
    created = sig.close_at.to_pydatetime() + dt.timedelta(seconds=1)
    opt = "CE" if sig.side == 1 else "PE"
    strike = round(sig.price / float(spec.strike_step or 50)) * float(spec.strike_step or 50)
    stop, target = sig.price - sig.side * risk, sig.price + sig.side * 3.0 * risk
    premium = 200.0
    return {
        "id": uuid.uuid4().hex[:12],
        "created_at": created.isoformat(),
        "expires_at": (created + dt.timedelta(minutes=10)).isoformat(),
        "instrument_key": spec.key, "series_key": instrument, "label": instrument,
        "kind": "INDEX", "side": f"BUY_{opt}", "direction": "long" if sig.side == 1 else "short",
        "strategy": STRATEGY, "strategy_label": STRATEGY, "timeframe": TIMEFRAME,
        "bar_ts": (sig.close_at - pd.Timedelta(minutes=3)).isoformat(),
        "strength": None, "confidence": {"score": 50.0}, "horizon": "intraday",
        "data": {"source": "dhan", "resolution": "1m", "delayed": False},
        "reasons": [], "text": "replay", "status": "PENDING", "risk_allowed": True,
        "economics": {
            "kind": "INDEX", "side": f"BUY_{opt}", "qty": int(spec.lot_size),
            "unit_label": f"1 lot ({spec.lot_size})", "underlying_entry": sig.price,
            "underlying_stop": stop, "underlying_target": target, "entry_price": premium,
            "stop_price": premium - DELTA * risk, "target_price": premium + DELTA * 3 * risk,
            "outlay": premium * spec.lot_size, "risk_inr": 0.0, "reward_inr": 0.0,
            "costs_inr": 0.0, "rr_net": None, "r_target": 3.0, "synthetic": True, "notes": [],
            "option": {"strike": strike, "opt_type": opt, "expiry": "2099-12-31",
                       "delta": DELTA, "iv": 0.15, "premium": premium,
                       "lot": int(spec.lot_size)},
        },
        "manage": {
            "target_r": 3.0, "trail_atr_mult": 0.0, "trail_activate_r": 1.0, "max_bars": 0,
            "exit_time": EXIT_TIME.strftime("%H:%M"), "atr": None, "tf_min": 3,
            "horizon": "intraday",
            "ratchet": {"stop_atr": 1.5, "atr_tf": "60m", "target_r": 3.0, "extend": True,
                        "risk": risk, "cost": cost},
        },
    }  # fmt: skip


def reference(
    sig: Signal, day: pd.DataFrame, risk: float, cost: float, params: ratchet.RatchetParams
) -> tuple[pd.Timestamp, str, float] | None:
    """The direct loop: (exit bar open time, reason, level), or None (no bar to fill on)."""
    created = sig.close_at + pd.Timedelta(seconds=1)
    rows = day[day.index > created]
    if rows.empty:
        return None
    plan = ratchet.plan_from_risk(sig.side, float(rows["open"].iloc[0]), risk, 3.0, cost)
    state = ratchet.start(plan)
    end = rows.index[0].normalize() + pd.Timedelta(hours=EXIT_TIME.hour, minutes=EXIT_TIME.minute)
    for ts, o, h, low in zip(rows.index, rows["open"], rows["high"], rows["low"], strict=True):
        hit = ratchet.exit_on_bar(plan, state, float(o), float(h), float(low))
        due = "SIGNAL_EXIT" if sig.exit_at is not None and ts >= sig.exit_at else None
        due = "SESSION_END" if ts >= end else due  # the session end ranks above it
        if hit is not None and hit[0].startswith("GAP_"):
            return ts, *_named(hit)
        if due is not None:
            return ts, due, float(o)
        if hit is not None:
            return ts, *_named(hit)
        state, _ = ratchet.advance(plan, state, float(h), float(low), params)
    last = rows.iloc[-1]
    return rows.index[-1] + pd.Timedelta(minutes=1), "SESSION_END", float(last["close"])


def _named(hit: tuple[str, float]) -> tuple[str, float]:
    reason, _ = rlive.close_reason(hit[0])
    return reason, hit[1]


def replay(store: Path, sessions: int, instrument: str) -> int:
    from onelot.paper.book import PaperBook
    from onelot.paper.money import to_paise
    from onelot.store import init_database

    found, minute = signals(store, instrument, sessions)
    _, instruments = load_specs()
    spec = instruments[slot_of(instrument)[0]]
    model = CostModel(CostConfig.load(ROOT / "config" / "costs.yaml").product(spec.product))
    settings = rlive.RatchetSettings()
    by_day = dict(list(minute.groupby(pd.DatetimeIndex(minute.index).date)))
    tmp = tempfile.TemporaryDirectory()
    db = init_database(f"sqlite:///{Path(tmp.name) / 'paper.db'}")

    def premium_at(option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float:
        sign = 1 if option["opt_type"] == "CE" else -1
        return max(0.05, 200.0 + DELTA * sign * (underlying - float(option["strike"])))

    book = PaperBook(
        db.engine, PaperCfg(enabled=True), BacktestCfg(), instruments,
        lambda key: model, premium_at, clock=lambda: dt.datetime.now(IST),
    )  # fmt: skip
    matched, differ, busy_until = 0, [], pd.Timestamp("2000-01-01", tz=IST)
    for sig in found:
        if sig.close_at < busy_until:
            continue  # one live idea at a time
        risk = rlive.first_risk(minute, sig.close_at, TIMEFRAME, settings)
        if not rlive.usable(risk):
            continue
        prem_cost = model.round_trip_cost(200.0, 200.0, int(spec.lot_size))
        cost = rlive.unit_cost(prem_cost, int(spec.lot_size), DELTA, option=True)
        alert = alert_for(sig, instrument, spec, risk, cost)
        day = by_day[sig.close_at.date()]
        want = reference(sig, day, risk, cost, settings.params)
        book.on_alert(alert, sig.close_at.to_pydatetime() + dt.timedelta(seconds=1))
        after = dt.datetime.combine(sig.close_at.date(), dt.time(15, 31), IST)
        exit_answer = None if sig.exit_at is None else ("SIGNAL_EXIT", sig.exit_at.to_pydatetime())
        events = book.step(after, lambda key, day=day: day, lambda trade, now, a=exit_answer: a)
        closed = [e for e in events if e.kind in ("CLOSED", "NO_FILL")]
        if want is None or not closed:
            differ.append(f"{sig.close_at}: no fill to compare (paper {[e.kind for e in events]})")
            continue
        got = closed[-1]
        busy_until = pd.Timestamp(got.ts)
        if got.kind == "NO_FILL":
            differ.append(f"{sig.close_at}: paper NO_FILL ({got.detail.get('reason')})")
            continue
        mine = (pd.Timestamp(got.ts), got.detail["reason"], got.detail["price_paise"])
        theirs = (want[0], want[1], to_paise(want[2]))
        if mine == theirs:
            matched += 1
        else:
            differ.append(f"{sig.close_at}: paper {mine} reference {theirs}")
    print(f"{instrument}: {len(found)} signals, {matched} paper exits match the direct loop")
    for line in differ:
        print(line)
    db.dispose()
    tmp.cleanup()
    print("RESULT:", "PASS" if not any("reference" in d for d in differ) else "MISMATCH")
    return 0 if not any("reference" in d for d in differ) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", type=Path, required=True, help="the SQLite bar store (read-only)")
    ap.add_argument("--sessions", type=int, default=20, help="the last N stored sessions")
    ap.add_argument("--instrument", default="NIFTY", help="the index (series key)")
    a = ap.parse_args(argv)
    if not a.store.exists():
        raise SystemExit(f"{a.store} does not exist")
    return replay(a.store, a.sessions, a.instrument.upper())


if __name__ == "__main__":
    raise SystemExit(main())
