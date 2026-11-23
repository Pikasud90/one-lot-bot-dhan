"""Dev tool: copy stored bars from a SOURCE database (opened read-only) into a DESTINATION
database that already has the schema, for a private test instance (US-083).

    python scripts/dev_seed_bars.py SRC.db DST.db --keys NIFTY,SBIN,INDIAVIX \
        --res 1m --from 2026-05-01 [--to 2026-11-14]
    python scripts/dev_seed_bars.py SRC.db DST.db --keys NIFTY --res 1d      (all days)

The stored `ts` is UTC. Every read is a range read on the index (series_key, resolution, ts),
so a 15 GB source is never scanned. The source is never written (`mode=ro`); rows already in
the destination are replaced. Writes nothing else: no broker, no network.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_KEYS = "NIFTY,BANKNIFTY,FINNIFTY,SENSEX,MIDCPNIFTY,RELIANCE,SBIN,ITC,TMPV,INDIAVIX"


def copy_bars(
    src: Path,
    dst: Path,
    keys: list[str],
    resolutions: list[str],
    start: str | None = None,
    end: str | None = None,
) -> dict[tuple[str, str], int]:
    """Copy bars with `start <= ts < end` (UTC text, either may be None) for each key and
    resolution. Returns the number of rows copied per (key, resolution)."""
    if src.resolve() == dst.resolve():
        raise ValueError("the source and the destination are the same file")
    ro = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True)
    rw = sqlite3.connect(str(dst))
    counts: dict[tuple[str, str], int] = {}
    try:
        for key in keys:
            for res in resolutions:
                sql = (
                    "SELECT ts, series_key, resolution, open, high, low, close, volume, oi, "
                    "source FROM bars WHERE series_key = ? AND resolution = ?"
                )
                args: list[str] = [key, res]
                if start:
                    sql += " AND ts >= ?"
                    args.append(start)
                if end:
                    sql += " AND ts < ?"
                    args.append(end)
                sql += " ORDER BY ts"
                n = 0
                cur = ro.execute(sql, args)
                while True:
                    rows = cur.fetchmany(20000)
                    if not rows:
                        break
                    rw.executemany(
                        "INSERT OR REPLACE INTO bars (ts, series_key, resolution, open, high,"
                        " low, close, volume, oi, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        rows,
                    )
                    n += len(rows)
                rw.commit()
                counts[(key, res)] = n
    finally:
        ro.close()
        rw.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--keys", default=DEFAULT_KEYS, help="comma-separated series keys")
    ap.add_argument("--res", default="1m,1d", help="comma-separated resolutions")
    ap.add_argument("--from", dest="start", default=None, help="UTC date or time, inclusive")
    ap.add_argument("--to", dest="end", default=None, help="UTC date or time, exclusive")
    a = ap.parse_args(argv)
    if not a.dst.is_file():
        print(
            "the destination must exist and have the schema (start the app once)", file=sys.stderr
        )
        return 2
    got = copy_bars(a.src, a.dst, a.keys.split(","), a.res.split(","), a.start, a.end)
    for (k, r), n in got.items():
        print(f"{k:12s} {r:3s} {n:8d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
