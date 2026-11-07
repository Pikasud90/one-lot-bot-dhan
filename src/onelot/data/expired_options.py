"""Dhan expired index options (`/charts/rollingoption`) in a local research store.

Dhan serves options on a ROLLING basis: per underlying, expiry flag (MONTH) and code
(1 = the near contract, 2 = the next one), CALL or PUT, and a strike RELATIVE to each
minute's at-the-money strike (ATM-10 .. ATM+10 for index options), one-minute bars that
carry the actual strike, IV, OI, volume and the underlying's spot. Checked against the live
API on 2026-11-07:

* bars are stamped at their START: Dhan's spot equals our own NIFTY one-minute CLOSE of the
  same stamp (2026-10-01, mean |diff| 0.4 points over 375 bars);
* a bar's strike is the strike nearest that minute's spot, plus rel x strike step;
* `expiryCode` 0 is refused ("expiryCode is required"); 1 is the near contract;
* only a ONE-day window is accepted (`toDate` = `fromDate` + 1; anything longer, even the
  docs' own one-month example, is refused with DH-905), and the answer holds BOTH days:
  `toDate` is inclusive in practice;
* a few bars carry an impossible strike (7,550 on a 44,000 BANKNIFTY) and prints after 15:30
  exist; both are dropped, never repaired;
* thinly traded monthly contracts (FINNIFTY, MIDCPNIFTY, early SENSEX) have minutes, days or
  whole months without a bar: a missing bar is missing, never invented.

A block is one trading day of one series (one request fills two), stored as a compressed
array blob in its own SQLite file (`data/expired_options.db`), so the 9 GB bar database and
the running desk are never touched. An empty answer is stored too, so a day without trades
is not asked again; a day that has not ended yet is fetched again when it is needed.
"""

from __future__ import annotations

import datetime as dt
import io
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..clock import IST

FIELDS = ("open", "high", "low", "close", "iv", "volume", "oi", "strike", "spot")
OPTS = {"CE": "CALL", "PE": "PUT"}
MAX_REL = 10  # Dhan: up to ATM+-10 for index options on the near contracts
SESSION_OPEN_MIN = 9 * 60 + 15
SESSION_CLOSE_MIN = 15 * 60 + 30  # a bar opening at 15:30 or later is a closing print: dropped
MAX_STRIKE_GAP = 0.30  # a strike more than 30 % from the spot is a bad print


class RollingSource(Protocol):
    def rolling_option(
        self,
        underlying_security_id: str | int,
        fno_segment: str,
        expiry_code: int,
        strike: str,
        option_type: str,
        start: dt.date,
        end: dt.date,
        expiry_flag: str = "MONTH",
        interval_min: int = 1,
    ) -> dict: ...


@dataclass(frozen=True)
class Underlying:
    """What Dhan needs to find an index's options, and the strike grid."""

    key: str  # series key: NIFTY, BANKNIFTY ...
    security_id: str
    fno_segment: str  # NSE_FNO | BSE_FNO
    strike_step: float

    @classmethod
    def from_specs(cls, series: Any, instrument: Any) -> Underlying:
        """From the universe's SeriesSpec (index) and InstrumentSpec (its options)."""
        if not series.dhan_security_id:
            raise ValueError(f"{series.key}: no Dhan security id in universe.yaml")
        step = float(instrument.strike_step or 0)
        if step <= 0:
            raise ValueError(f"{instrument.key}: no strike step in universe.yaml")
        return cls(series.key, str(series.dhan_security_id), f"{series.exchange}_FNO", step)


@dataclass(frozen=True)
class RollingKey:
    """One rolling series: an underlying's near (1) or next (2) monthly CE/PE at ATM+rel."""

    underlying: str
    code: int
    opt: str  # CE | PE
    rel: int
    flag: str = "MONTH"

    def __post_init__(self) -> None:
        if self.opt not in OPTS:
            raise ValueError(f"opt must be CE or PE, not {self.opt!r}")
        if self.code not in (1, 2, 3):
            raise ValueError(f"expiry code must be 1, 2 or 3, not {self.code!r}")
        if abs(int(self.rel)) > MAX_REL:
            raise ValueError(f"rel {self.rel} is outside ATM-{MAX_REL}..ATM+{MAX_REL}")

    @property
    def strike_arg(self) -> str:
        return "ATM" if self.rel == 0 else f"ATM{int(self.rel):+d}"


def parse_leg(leg: dict) -> dict[str, np.ndarray]:
    """Dhan's column lists -> arrays: ts (int64 epoch seconds) and every field (float64,
    NaN where Dhan sent nothing). Lists shorter than the timestamps are padded with NaN."""
    ts_raw = leg.get("timestamp") or []
    n = len(ts_raw)
    out: dict[str, np.ndarray] = {"ts": np.asarray(ts_raw, dtype="int64").reshape(-1)}
    for name in FIELDS:
        col = np.full(n, np.nan)
        raw = leg.get(name)
        if isinstance(raw, list):
            vals = np.asarray(
                [float(v) if isinstance(v, (int, float)) else np.nan for v in raw[:n]], dtype=float
            )
            col[: len(vals)] = vals
        out[name] = col
    return out


def sanitize(arr: dict[str, np.ndarray], first: dt.date, last: dt.date) -> dict[str, np.ndarray]:
    """Keep the bars a researcher can trust, inside [first, last] (IST dates):

    09:15 <= open time < 15:30; open, high, low, close finite and positive with low <= open,
    close <= high; a strike within 30 % of the spot; one bar per stamp (the last), sorted."""
    ts = arr["ts"]
    if ts.size == 0:
        return _empty()
    ist = ts + 19800  # IST seconds of the epoch
    days = ist // 86400
    minute = (ist % 86400) // 60
    d0 = (dt.date(first.year, first.month, first.day) - dt.date(1970, 1, 1)).days
    d1 = (dt.date(last.year, last.month, last.day) - dt.date(1970, 1, 1)).days
    o, h, lo, c = arr["open"], arr["high"], arr["low"], arr["close"]
    k, spot = arr["strike"], arr["spot"]
    with np.errstate(invalid="ignore"):
        ok = (
            (days >= d0)
            & (days <= d1)
            & (minute >= SESSION_OPEN_MIN)
            & (minute < SESSION_CLOSE_MIN)
            & np.isfinite(o)
            & np.isfinite(h)
            & np.isfinite(lo)
            & np.isfinite(c)
            & (o > 0)
            & (h > 0)
            & (lo > 0)
            & (c > 0)
            & (lo <= np.minimum(o, c) + 1e-9)
            & (h >= np.maximum(o, c) - 1e-9)
            & np.isfinite(k)
            & np.isfinite(spot)
            & (k > 0)
            & (spot > 0)
            & (np.abs(k - spot) <= MAX_STRIKE_GAP * spot)
        )
    keep = {name: v[ok] for name, v in arr.items()}
    if keep["ts"].size == 0:
        return _empty()
    # one bar per stamp, the last one Dhan sent; ascending
    order = np.argsort(keep["ts"], kind="stable")
    keep = {name: v[order] for name, v in keep.items()}
    last_of = np.r_[keep["ts"][1:] != keep["ts"][:-1], True]
    return {name: v[last_of] for name, v in keep.items()}


def _empty() -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {"ts": np.zeros(0, dtype="int64")}
    out.update({name: np.zeros(0) for name in FIELDS})
    return out


def _pack(arr: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    cols: dict[str, Any] = {"ts": arr["ts"].astype("int64")}
    for name in FIELDS:
        # prices to the paisa survive float32 below 167,772; OI and volume need not be exact
        cols[name] = arr[name].astype("float32")
    np.savez_compressed(buf, **cols)
    return buf.getvalue()


def _unpack(blob: bytes | None) -> dict[str, np.ndarray]:
    if not blob:
        return _empty()
    with np.load(io.BytesIO(blob)) as z:
        out: dict[str, np.ndarray] = {"ts": z["ts"].astype("int64")}
        for name in FIELDS:
            v = z[name].astype(float)
            out[name] = np.round(v, 2) if name not in ("iv",) else v
        return out


@dataclass
class Block:
    key: RollingKey
    day: dt.date
    complete: bool  # the day had ended when it was fetched
    data: dict[str, np.ndarray]

    @property
    def rows(self) -> int:
        return int(self.data["ts"].size)


class OptionBarStore:
    """Month blocks of rolling option bars in their own SQLite file. Thread-safe."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS blocks (
                underlying TEXT NOT NULL, flag TEXT NOT NULL, code INTEGER NOT NULL,
                opt TEXT NOT NULL, rel INTEGER NOT NULL, day TEXT NOT NULL,
                rows INTEGER NOT NULL, complete INTEGER NOT NULL, fetched_at TEXT NOT NULL,
                data BLOB,
                PRIMARY KEY (underlying, flag, code, opt, rel, day)
            ) WITHOUT ROWID"""
        )
        self._db.commit()
        self._mem: dict[tuple, Block] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _pk(key: RollingKey, day: dt.date) -> tuple:
        return (key.underlying, key.flag, key.code, key.opt, int(key.rel), day)

    def has(self, key: RollingKey, day: dt.date) -> bool:
        """Whether any block (complete or of a day still running) is stored."""
        pk = self._pk(key, day)
        if pk in self._mem:
            return True
        with self._lock:
            row = self._db.execute(
                "SELECT complete FROM blocks WHERE underlying=? AND flag=? AND code=? AND opt=?"
                " AND rel=? AND day=?",
                (*pk[:5], pk[5].isoformat()),
            ).fetchone()
        return row is not None

    def get(self, key: RollingKey, day: dt.date) -> Block | None:
        pk = self._pk(key, day)
        hit = self._mem.get(pk)
        if hit is not None:
            return hit
        with self._lock:
            row = self._db.execute(
                "SELECT complete, data FROM blocks WHERE underlying=? AND flag=? AND code=?"
                " AND opt=? AND rel=? AND day=?",
                (*pk[:5], pk[5].isoformat()),
            ).fetchone()
        if row is None:
            return None
        block = Block(key, pk[5], bool(row[0]), _unpack(row[1]))
        if len(self._mem) > 20000:  # bounded: ~20000 day blocks of ~375 bars
            self._mem.clear()
        self._mem[pk] = block
        return block

    def put(self, block: Block) -> None:
        pk = self._pk(block.key, block.day)
        blob = _pack(block.data) if block.rows else None
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    *pk[:5],
                    pk[5].isoformat(),
                    block.rows,
                    int(block.complete),
                    dt.datetime.now(IST).isoformat(timespec="seconds"),
                    blob,
                ),
            )
            self._db.commit()
        self._mem[pk] = block

    def summary(self) -> list[tuple]:
        """(underlying, code, opt, blocks, empty blocks, bars) per series family."""
        with self._lock:
            return self._db.execute(
                "SELECT underlying, code, opt, COUNT(*), SUM(rows = 0), SUM(rows)"
                " FROM blocks GROUP BY underlying, code, opt ORDER BY underlying, code, opt"
            ).fetchall()


def fetch_days(
    source: RollingSource,
    und: Underlying,
    key: RollingKey,
    day: dt.date,
    today: dt.date | None = None,
) -> list[Block]:
    """One request (Dhan's only accepted window: `day` .. `day` + 1) -> the two day blocks,
    sanitized; a day with no bars is an empty block (a weekend, a holiday, no trades)."""
    if key.underlying != und.key:
        raise ValueError(f"key {key.underlying} does not match underlying {und.key}")
    today = today or dt.datetime.now(IST).date()
    if day > today:  # nothing has traded yet: no request, no block
        return []
    nxt = day + dt.timedelta(days=1)
    leg = source.rolling_option(
        und.security_id, und.fno_segment, key.code, key.strike_arg, OPTS[key.opt], day, nxt,
        key.flag,
    )  # fmt: skip
    arr = parse_leg(leg) if leg else _empty()
    return [
        Block(key, d, complete=d < today, data=sanitize(arr, d, d))
        for d in (day, nxt)
        if d <= today
    ]


def ensure_blocks(
    source: RollingSource,
    store: OptionBarStore,
    unds: dict[str, Underlying],
    needs: Iterable[tuple[RollingKey, dt.date]],
    progress: Any = None,
    today: dt.date | None = None,
) -> dict[str, int]:
    """Fetch every needed day block the store does not hold (a day still running again).
    A request also stores the next day, so a need it covered is not asked for twice.
    Returns counts: requests, empty (blocks), cached, failed."""
    counts = {"requests": 0, "empty": 0, "cached": 0, "failed": 0}
    today = today or dt.datetime.now(IST).date()
    todo = []
    for key, day in dict.fromkeys(needs):
        if day > today:
            continue  # a day that has not happened is never asked for
        blk = store.get(key, day)
        if blk is not None and blk.complete:
            counts["cached"] += 1
        else:
            todo.append((key, day))
    for i, (key, day) in enumerate(todo):
        blk = store.get(key, day)
        if blk is not None and blk.complete:  # the previous request brought it
            counts["cached"] += 1
            continue
        try:
            blocks = fetch_days(source, unds[key.underlying], key, day, today)
        except Exception as exc:  # noqa: BLE001 - one bad day never stops a backfill
            counts["failed"] += 1
            if progress:
                progress(i + 1, len(todo), f"{key} {day} FAILED: {exc}")
            continue
        counts["requests"] += 1
        for b in blocks:
            store.put(b)
            counts["empty"] += int(b.rows == 0)
        if progress:
            progress(i + 1, len(todo), f"{key} {day}: {blocks[0].rows if blocks else 0} bars")
    return counts


def minute_index(block: Block) -> dict[int, int]:
    """epoch second of a bar's start -> row, for O(1) lookups."""
    return {int(t): i for i, t in enumerate(block.data["ts"])}


def ist_epoch(ts: Any) -> int:
    """An aware datetime/Timestamp -> epoch seconds (the store's stamp)."""
    if hasattr(ts, "timestamp"):
        return int(round(ts.timestamp()))
    raise TypeError(f"not a timestamp: {ts!r}")


__all__ = [
    "Block",
    "OptionBarStore",
    "RollingKey",
    "Underlying",
    "ensure_blocks",
    "fetch_days",
    "ist_epoch",
    "minute_index",
    "parse_leg",
    "sanitize",
]
