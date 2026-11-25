"""Copy option day blocks a target store lacks from another store of the same schema.

    .venv/bin/python scripts/merge_option_bars.py --from FILE --into FILE [--dry-run]

Both files are `OptionBarStore` files (`data/expired_options.db` and research copies such as
`data/research/real_puts_2026-11-25/options.sqlite`): one table `blocks`, one row per series
(underlying, flag, code, CE or PE, offset from the money) and day. A block the target already
holds is NEVER replaced, whatever either side holds (a difference is counted and printed); only
missing blocks are copied, one transaction a day, so a stopped merge leaves whole days and can
simply be run again. The source is opened read only. `--dry-run` prints what would be added and
writes nothing. No network.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PK = ("underlying", "flag", "code", "opt", "rel", "day")
COLUMNS = (*PK, "rows", "complete", "fetched_at", "data")
MISSING = (
    "NOT EXISTS (SELECT 1 FROM main.blocks t WHERE t.underlying = s.underlying AND t.flag ="
    " s.flag AND t.code = s.code AND t.opt = s.opt AND t.rel = s.rel AND t.day = s.day)"
)


def columns(db: sqlite3.Connection, schema: str) -> tuple[str, ...]:
    return tuple(r[1] for r in db.execute(f"PRAGMA {schema}.table_info(blocks)"))


def merge(src: Path, dst: Path, dry_run: bool = False, out=print) -> dict[str, int]:  # noqa: ANN001
    """Copy the blocks `dst` lacks from `src`; returns counts (blocks, bars, days, held,
    differing). With `dry_run` nothing is written."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if src == dst:
        raise SystemExit("--from and --into are the same file")
    if not src.is_file() or not dst.is_file():
        raise SystemExit("both files must exist")
    db = sqlite3.connect(f"{dst.as_uri()}{'?mode=ro' if dry_run else ''}", uri=True, timeout=30)
    db.execute("ATTACH DATABASE ? AS s", (f"{src.as_uri()}?mode=ro",))
    if columns(db, "main") != COLUMNS or columns(db, "s") != COLUMNS:
        raise SystemExit("both files must hold the option store's `blocks` table")
    held = db.execute(
        "SELECT COUNT(*), SUM(t.rows != s.rows OR t.complete != s.complete OR"
        " COALESCE(t.data, x'') != COALESCE(s.data, x'')) FROM s.blocks s JOIN main.blocks t"
        " USING (underlying, flag, code, opt, rel, day)"
    ).fetchone()
    plan = db.execute(
        f"SELECT underlying, opt, COUNT(*), SUM(rows > 0), SUM(rows), COUNT(DISTINCT day)"
        f" FROM s.blocks s WHERE {MISSING} GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    days = [d for (d,) in db.execute(
        f"SELECT DISTINCT day FROM s.blocks s WHERE {MISSING} ORDER BY day")]  # fmt: skip
    counts = {"blocks": sum(r[2] for r in plan), "bars": sum(r[4] or 0 for r in plan),
              "days": len(days), "held": int(held[0] or 0), "differing": int(held[1] or 0)}  # fmt: skip
    out(f"{'would add' if dry_run else 'adding'} {counts['blocks']} blocks "
        f"({counts['bars']} bars) on {counts['days']} days; already held {counts['held']}, of "
        f"which differing {counts['differing']} (kept as they are)")  # fmt: skip
    for und, opt, n, nonempty, bars, nd in plan:
        out(f"  {und:10s} {opt}: {n} blocks ({nonempty} with bars, {bars or 0} bars) on {nd} days")
    if dry_run:
        db.close()
        return counts
    cols = ", ".join(COLUMNS)
    done = 0
    for d in days:
        with db:  # one transaction a day
            db.execute(
                f"INSERT INTO main.blocks ({cols}) SELECT {cols} FROM s.blocks s WHERE s.day = ?"
                f" AND {MISSING}",
                (d,),
            )
        done += 1
    db.close()
    out(f"done: {done} days written")
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", required=True, help="the store to copy from")
    ap.add_argument("--into", dest="dst", required=True, help="the store to copy into")
    ap.add_argument("--dry-run", action="store_true", help="print what would be added")
    a = ap.parse_args()
    merge(Path(a.src), Path(a.dst), a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
