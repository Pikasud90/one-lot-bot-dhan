"""The pullback rule's wide list (US-099): `config/delivery_universe.yaml` and its daily bars.

The list is data, not code: each stock's key (the series key of its daily bars in the store),
name, the symbol Dhan shows, Dhan's security id and segment, and its group (desk, alpha or new).
A stock that is not a desk instrument is DELIVERY-ONLY: daily bars only, no feed, no card.

`prepare_daily` is what the import tool writes and the replay reads: a research file's daily
bars with the study's own repairs (`scripts/swing_universe.py`, `load_raw`): bars with a price
that is missing or not positive left out, and five corporate actions Dhan's daily bars do not
adjust put on the later scale by that day's close ratio.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd  # type: ignore[import-untyped]
import yaml

from ..clock import IST

GROUPS = ("desk", "alpha", "new")
#: the study's corporate actions on Dhan's daily bars (stage `check` of the study)
CORP_ACTIONS: dict[str, tuple[str, ...]] = {
    "ADANIENT": ("2015-06-03",),
    "JSL": ("2015-11-19",),
    "TATACHEM": ("2020-03-04",),
    "BRITANNIA": ("2010-03-08",),
    "TMPV": ("2011-01-03",),
}
#: a daily bar is stamped at the session's open, as the Dhan provider stamps it
DAILY_AT = dt.time(9, 15)


@dataclass(frozen=True)
class UniverseStock:
    key: str
    name: str
    symbol: str
    security_id: str
    segment: str
    group: str


@dataclass(frozen=True)
class Universe:
    stocks: tuple[UniverseStock, ...]
    backtest: dict[str, Any]

    def delivery_only(self, desk_keys: Iterable[str]) -> tuple[UniverseStock, ...]:
        """The stocks that are not desk instruments (no feed, no card, daily bars only)."""
        desk = {k.upper() for k in desk_keys}
        return tuple(s for s in self.stocks if s.key.upper() not in desk)


def load_universe(path: str | Path) -> Universe:
    """The wide list; an unknown group, a missing field or a key given twice is an error."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = raw.get("stocks") or []
    if not isinstance(rows, list):
        raise ValueError(f"{path}: stocks must be a list")
    out: list[UniverseStock] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError(f"{path}: a stock must be a mapping, not {r!r}")
        try:
            s = UniverseStock(
                str(r["key"]).upper(),
                str(r.get("name") or r["key"]),
                str(r.get("symbol") or r["key"]),
                str(r["security_id"]),
                str(r.get("segment") or "NSE_EQ"),
                str(r["group"]),
            )
        except KeyError as exc:
            raise ValueError(f"{path}: a stock lacks {exc}: {r!r}") from exc
        if s.group not in GROUPS:
            raise ValueError(f"{path}: {s.key}: group must be one of {GROUPS}, not {s.group!r}")
        if s.key in seen:
            raise ValueError(f"{path}: {s.key} is listed twice")
        seen.add(s.key)
        out.append(s)
    return Universe(tuple(out), dict(raw.get("backtest") or {}))


def read_only(path: str | Path) -> sqlite3.Connection:
    """An SQLite file opened read-only (URI mode=ro): nothing in it can be written."""
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def research_daily(con: sqlite3.Connection, key: str) -> pd.DataFrame:
    """One stock's daily bars from a research file (table `daily`: symbol, date, open, high,
    low, close, volume), indexed by date (dt.date), as stored."""
    rows = con.execute(
        "SELECT date, open, high, low, close, volume FROM daily WHERE symbol = ? ORDER BY date",
        (key,),
    ).fetchall()
    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    frame.index = [dt.date.fromisoformat(str(d)) for d in frame.pop("date")]
    return frame.astype(float)


def prepare_daily(key: str, frame: pd.DataFrame) -> pd.DataFrame:
    """The study's repairs: positive prices only, then the listed corporate actions rescaled
    (prices before the day times that day's close ratio, volume divided by it)."""
    f = frame[(frame.open > 0) & (frame.high > 0) & (frame.low > 0) & (frame.close > 0)].copy()
    for day in CORP_ACTIONS.get(key, ()):
        d = dt.date.fromisoformat(day)
        if d not in f.index:
            continue
        i = list(f.index).index(d)
        if i == 0:
            continue
        k = float(f.close.iloc[i]) / float(f.close.iloc[i - 1])
        f.iloc[:i, :4] = f.iloc[:i, :4].to_numpy() * k
        f.iloc[:i, 4] = f.iloc[:i, 4].to_numpy() / k
    return f


def stamped(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily bars indexed by date, re-indexed at 09:15 IST as the store keeps daily bars."""
    out = frame.copy()
    out.index = pd.DatetimeIndex(
        [dt.datetime.combine(d, DAILY_AT, tzinfo=IST) for d in frame.index]
    )
    return out
