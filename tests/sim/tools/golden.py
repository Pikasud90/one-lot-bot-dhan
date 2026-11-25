"""The Simulator driven the same way every time, for the sameness tests of US-095.

`PRESENT` sessions on fixture bars are driven by `drive`: advances in a fixed cycle of chunk
sizes, every alert taken at the end of the chunk that shows it, every third trade closed by
hand, until `MINUTES` minutes are revealed or the data ends. What came out (every minute,
every event, every alert and trade seen) is the record; `tests/sim/golden/present_engine.json`
holds the record of the engine as it was BEFORE US-095 (commit 8e854d2), made with

    PYTHONPATH=src:tests .venv/bin/python tests/sim/tools/golden.py write

and `tests/sim/test_sameness.py` compares the rebuilt engine with it. Not collected by pytest.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from onelot.clock import IST

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "present_engine.json"
CHUNKS = (1, 7, 30, 3, 13, 1, 60, 2, 5, 390)
MINUTES = 375 * 34  # 34 trading days: past the old engine's 30-day reload and two blocks
DATA_FROM = "2025-01-06"
DATA_DAYS = 130
START_DAY = 80  # the trading day the sessions start on

#: (instrument, strategy, timeframe): five pairs, index and stock, single and multi timeframe
PRESENT: tuple[tuple[str, str, str], ...] = (
    ("NIFTY", "microtrend", "3m"),
    ("NIFTY", "orb", "5m"),
    ("BANKNIFTY", "supertrend_adx", "15m"),
    ("RELIANCE", "vwap_pullback", "5m"),
    ("RELIANCE", "ema_cross", "5m"),
)


def fixture_frames() -> dict[str, pd.DataFrame]:
    from sim.conftest import walk_minutes

    return {
        "NIFTY": walk_minutes(DATA_FROM, DATA_DAYS, 25000.0, 0.0006, seed=31, volume=False),
        "BANKNIFTY": walk_minutes(DATA_FROM, DATA_DAYS, 52000.0, 0.0007, seed=32, volume=False),
        "RELIANCE": walk_minutes(DATA_FROM, DATA_DAYS, 1300.0, 0.0008, seed=33, volume=True),
    }


def start_iso(day: int = START_DAY) -> str:
    from sim.conftest import trading_days

    d = trading_days(DATA_FROM, DATA_DAYS)[day]
    return pd.Timestamp(f"{d} 10:02", tz=IST).isoformat()


def body(inst: str, strategy: str, tf: str, start: str | None = None) -> dict[str, Any]:
    return {"instrument": inst, "strategy": strategy, "timeframe": tf, "mode": "past",
            "start": start or start_iso(), "params": {}}  # fmt: skip


def _clean(e: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in e.items() if k in ("seq", "ts", "kind", "text", "data")}


def _trade(t: dict[str, Any] | None) -> dict[str, Any] | None:
    if t is None:
        return None
    keep = ("id", "status", "side", "action", "qty", "entry_ts", "entry", "entry_price", "source",
            "stop", "target", "stop0", "target0", "stage", "best", "progress", "mark",
            "mark_price", "open_pnl", "exit_ts", "exit", "exit_price", "exit_reason", "gross",
            "charges", "net", "r_multiple")  # fmt: skip
    return {k: t.get(k) for k in keep}


def _alert(a: dict[str, Any] | None) -> dict[str, Any] | None:
    if a is None:
        return None
    keep = ("id", "ts", "expires", "side", "action", "entry", "stop", "target", "risk",
            "reward", "qty", "traded", "risk_inr", "reward_inr", "costs_inr", "reasons")  # fmt: skip
    return {k: a.get(k) for k in keep}


def drive(
    session: Any,
    minutes: int = MINUTES,
    chunks: tuple[int, ...] = CHUNKS,
    act: Any = None,
) -> dict[str, Any]:
    """Drive a `SimSession` (or anything with its `advance`, `take` and `close`) and record
    what it showed. `act(session, kind, *args)` replaces the direct calls (the API tests pass
    one that goes through HTTP)."""

    def call(kind: str, *args: Any) -> dict[str, Any]:
        if act is not None:
            return act(session, kind, *args)
        return getattr(session, kind)(*args)

    rec: dict[str, Any] = {"minutes": [], "events": [], "alerts": [], "trades": [], "ends": []}
    shown = 0
    taken = 0
    i = 0
    while shown < minutes:
        n = chunks[i % len(chunks)]
        i += 1
        r = call("advance", n)
        rec["minutes"].extend(r["minutes"])
        rec["events"].extend(_clean(e) for e in r["events"])
        shown += len(r["minutes"])
        rec["ends"].append([r["cursor"], _trade(r["trade"]), r["summary"]])
        if r["over"]:
            break
        a = r["alert"]
        if a is not None and r["trade"] is None:
            rec["alerts"].append(_alert(a))
            got = call("take", a["id"])
            rec["events"].extend(_clean(e) for e in got["events"])
            taken += 1
        elif r["trade"] is not None and r["trade"]["status"] == "OPEN" and taken % 3 == 0:
            got = call("close")
            rec["events"].extend(_clean(e) for e in got["events"])
    final = call("end")
    rec["events"].extend(_clean(e) for e in final["events"])
    rec["minutes"].extend(final["minutes"])
    rec["trades"] = [_trade(t) for t in final["trades"]]
    rec["summary"] = final["summary"]
    return rec


def digest(rec: dict[str, Any]) -> dict[str, Any]:
    """The record with the minutes replaced by their count and hash (they are thousands)."""
    m = json.dumps(rec["minutes"], separators=(",", ":"))
    out = {k: v for k, v in rec.items() if k != "minutes"}
    out["minutes"] = {"n": len(rec["minutes"]), "sha1": hashlib.sha1(m.encode()).hexdigest()}
    return out


def record_all(manager_for: Any) -> dict[str, Any]:
    """{"NIFTY|microtrend|3m": digest} for every pair of `PRESENT`; `manager_for()` gives a
    manager on the fixture bars with a fresh database."""
    out = {}
    for inst, strategy, tf in PRESENT:
        m = manager_for()
        made = m.create(body(inst, strategy, tf))
        s = m.get(made["session"]["id"])
        out[f"{inst}|{strategy}|{tf}"] = digest(drive(s))
    return out


def _main() -> None:
    sys.path.insert(0, str(ROOT.parent))
    from sim.conftest import MemBars, make_manager

    from onelot.store import init_database

    frames = fixture_frames()
    tmp = Path(tempfile.mkdtemp())
    count = [0]

    def manager_for() -> Any:
        count[0] += 1
        db = init_database(f"sqlite:///{tmp / f'g{count[0]}.db'}")
        return make_manager(db.engine, MemBars(frames), tmp)

    got = record_all(manager_for)
    if sys.argv[1:] == ["write"]:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True))
        print("wrote", GOLDEN)
    for k, v in got.items():
        kinds: dict[str, int] = {}
        for e in v["events"]:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        print(k, v["minutes"]["n"], "minutes", len(v["trades"]), "trades", kinds)


if __name__ == "__main__":
    _main()
