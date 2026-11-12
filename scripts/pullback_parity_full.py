"""US-067 whole-window desk parity (the tester's driver, kept as a script: about 4 minutes).

Every session of the fixtures (2017-01-02 to 2026-11-06) goes through the product path,
`onelot.delivery.runner.DeliveryDesk`, with the shipped settings: the stored days before the
session, and that session (and the one before) as two one-minute bars, open and close, as its
live bars; polled after the close. The trades are compared with the reference's 247.

    PYTHONPATH=src .venv/bin/python -W ignore scripts/pullback_parity_full.py [sessions]

The fixture bars are already cleaned (`data/hygiene`), so the live bars are not cleaned again
here: the ITC and Tata Motors back-adjustment is keyed by date and would apply twice.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import onelot.delivery.data as data  # noqa: E402
from onelot.clock import IST, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.delivery.runner import DeliveryDesk  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "pullback"


class _Quiet:
    def send(self, n, dedup_key=None):  # noqa: ANN001, ANN201
        return {}


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    data.clean_intraday = lambda key, frame: frame  # the fixtures are cleaned already
    cfg = load_desk_config()
    keys = [i.key for i, _ in cfg.delivery_slots()] + ["NIFTY"]
    raw = {k: pd.read_csv(FIX / f"daily_{k}.csv.gz") for k in keys}
    daily, dates, rows = {}, {}, {}
    for k, b in raw.items():
        idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in b["date"]])
        daily[k] = b.drop(columns=["date"]).set_index(idx)
        dates[k] = np.array([dt.date.fromisoformat(d) for d in b["date"]])
        rows[k] = {
            dt.date.fromisoformat(d): (o, c)
            for d, o, c in zip(b["date"], b["open"], b["close"], strict=True)
        }
    window = list(dates["NIFTY"])
    if len(sys.argv) > 1:
        window = window[: int(sys.argv[1])]

    def fine(k: str, day: dt.date) -> pd.DataFrame | None:
        if day not in rows[k]:
            return None
        o, c = rows[k][day]
        last = dt.time(15, 29) if k == "NIFTY" else dt.time(15, 14)
        idx = pd.DatetimeIndex(
            [
                dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST),
                dt.datetime.combine(day, last, tzinfo=IST),
            ]
        )
        return pd.DataFrame(
            {
                "open": [o, o],
                "high": [max(o, c)] * 2,
                "low": [min(o, c)] * 2,
                "close": [o, c],
                "volume": 1.0,
            },
            index=idx,
        )

    desk = DeliveryDesk(
        create_engine("sqlite://"), cfg, CostConfig.load(ROOT / "config" / "costs.yaml"),
        SessionCfg(), TradingCalendar({}, verified=True), notifier=_Quiet(),
        started_at=dt.datetime(2000, 1, 1, tzinfo=IST), background=False,
    )  # fmt: skip
    t0 = time.perf_counter()
    for i, day in enumerate(window):
        prev = window[i - 1] if i else None
        for k in keys:
            desk._stored[k] = (daily[k].iloc[: int(np.searchsorted(dates[k], day))], {})
        desk._history_day = day

        def bars(key: str, day: dt.date = day, prev: dt.date | None = prev) -> pd.DataFrame:
            parts = [fine(key, d) for d in (prev, day) if d is not None]
            parts = [p for p in parts if p is not None]
            return pd.concat(parts) if parts else pd.DataFrame()

        now = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 30), tzinfo=IST)
        desk.on_poll(now, bars, lambda key: None)
        if i % 250 == 0:
            print(f"session {i}: {day} ({time.perf_counter() - t0:.0f} s)")
    got = {(t.series_key, t.signal_date.isoformat()): t for t in desk.book._select()}
    ref = pd.read_csv(FIX / "expected_trades.csv")
    if len(sys.argv) > 1:
        ref = ref[ref.signal_date <= window[-2].isoformat()]
    matched, diffs = 0, []
    for r in ref.itertuples():
        t = got.pop((r.stock, r.signal_date), None)
        if t is None:
            diffs.append(f"only in the reference: {r.stock} {r.signal_date}")
            continue
        eo = None if t.entry_paise is None else t.entry_paise / 100 / 1.0003
        ok = (
            t.entry_session is not None
            and t.entry_session.isoformat() == r.entry_date
            and eo is not None
            and abs(eo - r.entry_open) < 0.01
        )
        if r.open_at_end:
            ok = ok and t.status in ("OPEN", "EXIT_PENDING")
        else:
            xo = None if t.exit_paise is None else t.exit_paise / 100 / 0.9997
            ok = ok and t.status == "CLOSED" and t.exit_ts is not None
            ok = ok and t.exit_ts.date().isoformat() == r.exit_date
            ok = ok and xo is not None and abs(xo - r.exit_price) < 0.01
        if ok:
            matched += 1
        else:
            diffs.append(f"differs: {r.stock} {r.signal_date} ({t.status})")
    diffs += [f"only in the replay: {k} {d} ({t.status})" for (k, d), t in sorted(got.items())]
    print(f"reference trades: {len(ref)}; matched: {matched}")
    print("\n".join(diffs))
    print(f"DIFFS: {len(diffs)}; {time.perf_counter() - t0:.0f} s for {len(window)} sessions")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
