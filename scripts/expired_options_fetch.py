"""Fetch Dhan expired-option day blocks into data/expired_options.db (read-only Data API).

Dhan accepts only a one-day window (`day`..`day`+1, answered with both days), so a range costs
one request per two calendar days per series: a year of one series is ~180 requests. The
real-price study (`scripts/real_options_study.py`) fetches only the days its trades need; use
this script for a bulk range.

Usage:
  .venv/bin/python scripts/expired_options_fetch.py --start 2026-10-01 --end 2026-10-31 \
      [--series NIFTY,BANKNIFTY] [--codes 1,2] [--opts CE,PE] [--rels -2,-1,0,1,2] [--rate 3]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onelot.broker.dhan import DhanAdapter  # noqa: E402
from onelot.clock import IST  # noqa: E402
from onelot.config import Secrets  # noqa: E402
from onelot.data.expired_options import (  # noqa: E402
    OptionBarStore,
    RollingKey,
    Underlying,
    ensure_blocks,
)
from onelot.data.universe import load_specs  # noqa: E402
from onelot.ratelimit import EndpointSpec, RateLimiter  # noqa: E402

INDEX_OPTIONS = {
    "BANKNIFTY": "BANKNIFTY_OPT",
    "NIFTY": "NIFTY_OPT",
    "FINNIFTY": "FINNIFTY_OPT",
    "MIDCPNIFTY": "MIDCPNIFTY_OPT",
    "SENSEX": "SENSEX_OPT",
}
STORE_PATH = ROOT / "data" / "expired_options.db"


def research_adapter(rate: float) -> DhanAdapter:
    """The app's own Dhan adapter (credentials from .env, never printed) with a research
    rate on the data bucket: Dhan allows 5 data requests a second; we stay under."""
    s = Secrets.load(ROOT)
    limiter = RateLimiter((EndpointSpec("historical", rate_per_sec=rate, capacity=rate),))
    return DhanAdapter(s.client_id, s.data_token, limiter)


def underlyings() -> dict[str, Underlying]:
    series, insts = load_specs()
    return {k: Underlying.from_specs(series[k], insts[v]) for k, v in INDEX_OPTIONS.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default=",".join(INDEX_OPTIONS))
    ap.add_argument("--codes", default="1,2")
    ap.add_argument("--opts", default="CE")
    ap.add_argument("--rels", default="0")
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    ap.add_argument("--end", help="last day (default: today)")
    ap.add_argument("--rate", type=float, default=3.0)
    a = ap.parse_args()
    unds = underlyings()
    today = dt.datetime.now(IST).date()
    first = dt.date.fromisoformat(a.start)
    last = min(dt.date.fromisoformat(a.end), today) if a.end else today
    # every other calendar day: each request also returns the next day
    days = [first + dt.timedelta(days=i) for i in range(0, (last - first).days + 1, 2)]
    needs = [
        (RollingKey(s, int(c), o, int(r)), d)
        for s in a.series.split(",")
        for c in a.codes.split(",")
        for o in a.opts.split(",")
        for r in a.rels.split(",")
        for d in days
    ]
    store = OptionBarStore(STORE_PATH)
    t0 = time.time()

    def progress(i: int, n: int, msg: str) -> None:
        if i % 25 == 0 or i == n or "FAILED" in msg:
            print(f"[{time.time() - t0:6.0f}s] {i}/{n} {msg}", flush=True)

    counts = ensure_blocks(research_adapter(a.rate), store, unds, needs, progress, today)
    print("done", counts, f"in {time.time() - t0:.0f}s", flush=True)
    for row in store.summary():
        print("  ", row)


if __name__ == "__main__":
    main()
