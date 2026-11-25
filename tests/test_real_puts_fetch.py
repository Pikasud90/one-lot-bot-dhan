"""US-098 fetcher (`scripts/real_puts_fetch.py`): resume, pacing, refusals, the exit strike.

No network: the source is a fake with Dhan's `rolling_option` signature.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

from onelot.clock import IST
from onelot.data.expired_options import OptionBarStore, RollingKey, Underlying

ROOT = Path(__file__).resolve().parents[1]


def _load():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("real_puts_fetch_t", ROOT / "scripts" /
                                                  "real_puts_fetch.py")  # fmt: skip
    mod = importlib.util.module_from_spec(spec)
    sys.modules["real_puts_fetch_t"] = mod
    spec.loader.exec_module(mod)
    return mod


F = _load()
UND = {"NIFTY": Underlying("NIFTY", "13", "NSE_FNO", 50.0)}
MON = dt.date(2026, 10, 5)
NOW = dt.datetime(2026, 11, 25, 16, 0, tzinfo=IST)


def _epoch(d: dt.date, hh: int, mm: int) -> int:
    return int(dt.datetime.combine(d, dt.time(hh, mm), tzinfo=IST).timestamp())


class FakeDhan:
    """Answers like Dhan: the window `start` .. `start` + 1, both days, two bars a day."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, int, dt.date]] = []
        self.fail = fail

    def rolling_option(
        self,
        underlying_security_id,
        fno_segment,
        expiry_code,
        strike,  # noqa: ANN001
        option_type,
        start,
        end,
        expiry_flag="MONTH",
        interval_min=1,
    ) -> dict:  # noqa: ANN001
        assert end == start + dt.timedelta(days=1)
        self.calls.append((strike, expiry_code, start))
        if self.fail:
            raise RuntimeError("DH-901 refused")
        ts = [_epoch(d, 9, m) for d in (start, end) if d.weekday() < 5 for m in (20, 21)]
        n = len(ts)
        return {"timestamp": ts, "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                "close": [100.5] * n, "iv": [15.0] * n, "volume": [10] * n, "oi": [5] * n,
                "strike": [24000.0] * n, "spot": [24010.0] * n}  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def mono(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def _needs(days: int) -> list:
    key = RollingKey("NIFTY", 1, "PE", 0)
    return [(1, key, MON + dt.timedelta(days=i)) for i in range(days)]


def _run(tmp_path: Path, src: FakeDhan, needs: list, **kw):  # noqa: ANN202
    store = OptionBarStore(tmp_path / "options.sqlite")
    ledger = F.Ledger(tmp_path / "ledger.sqlite")
    clock = Clock()
    out = F.run_needs(src, store, UND, needs, ledger, F.held_by(store, kw.pop("others", [])),
                      now=lambda: NOW, mono=clock.mono, sleep=clock.sleep, **kw)  # fmt: skip
    store.close()
    return out, ledger, clock


def test_one_request_stores_two_days_and_a_rerun_asks_nothing_twice(tmp_path: Path) -> None:
    src = FakeDhan()
    (counts, why), ledger, _ = _run(tmp_path, src, _needs(5), cap=2)
    assert why == "cap" and counts["requests"] == 2
    assert [c[2] for c in src.calls] == [MON, MON + dt.timedelta(days=2)]
    (counts, why), ledger, _ = _run(tmp_path, src, _needs(5))
    assert why == "done" and counts["requests"] == 1 and counts["held"] == 4
    asked = [c[2] for c in src.calls]
    assert asked == [MON, MON + dt.timedelta(days=2), MON + dt.timedelta(days=4)]
    assert len(set(asked)) == len(asked)
    assert ledger.count_requests() == 3
    (counts, _), _, _ = _run(tmp_path, src, _needs(5))
    assert counts["requests"] == 0 and len(src.calls) == 3


def test_at_most_one_request_a_second(tmp_path: Path) -> None:
    src = FakeDhan()
    (counts, _), _, clock = _run(tmp_path, src, _needs(9))
    assert counts["requests"] == 5
    assert len(clock.slept) == 4 and all(s == pytest.approx(1.0) for s in clock.slept)


def test_three_refusals_in_a_row_stop_and_a_twice_refused_day_is_left(tmp_path: Path) -> None:
    src = FakeDhan(fail=True)
    (counts, why), ledger, _ = _run(tmp_path, src, _needs(10))
    assert why == "refused" and counts["requests"] == 3 and len(src.calls) == 3
    assert ledger.count_requests() == 3
    (counts, why), _, _ = _run(tmp_path, src, _needs(10))
    assert why == "refused" and len(src.calls) == 6
    # the first three days are now refused twice: a third run leaves them and asks the rest
    (counts, _), _, _ = _run(tmp_path, src, _needs(10))
    assert counts["given_up"] == 3
    assert src.calls[6][2] == MON + dt.timedelta(days=3)


def test_deadline_stops_before_asking(tmp_path: Path) -> None:
    src = FakeDhan()
    (counts, why), _, _ = _run(tmp_path, src, _needs(3), deadline=NOW)
    assert why == "deadline" and counts["requests"] == 0 and not src.calls


def test_a_day_the_app_store_holds_is_not_asked(tmp_path: Path) -> None:
    other = OptionBarStore(tmp_path / "app.sqlite")
    key = RollingKey("NIFTY", 1, "PE", 0)
    from onelot.data.expired_options import fetch_days

    for b in fetch_days(FakeDhan(), UND["NIFTY"], key, MON, NOW.date()):
        other.put(b)
    other.close()
    src = FakeDhan()
    (counts, _), _, _ = _run(tmp_path, src, _needs(2),
                             others=[F.ReadOnlyBlocks(tmp_path / "app.sqlite")])  # fmt: skip
    assert counts["held"] == 2 and not src.calls


def test_ledger_keeps_the_lower_tier(tmp_path: Path) -> None:
    ledger = F.Ledger(tmp_path / "ledger.sqlite")
    key = RollingKey("NIFTY", 1, "CE", -2)
    assert ledger.add_needs([(6, key, MON, "late")]) == 1
    ledger.add_needs([(3, key, MON, "early"), (7, key, MON, "stop")])
    assert ledger.needs() == [(3, key, MON)]


def test_exit_strike_offset() -> None:
    # bought at 24,000 at the money; at the exit the money is at 24,100: the strike is ATM-2
    assert F.exit_rel(24000.0, 24100.0, 50.0) == -2
    assert F.exit_rel(52000.0, 51700.0, 100.0) == 3
    assert F.exit_rel(52000.0, 51750.0, 100.0) is None

    def atm(minute: int) -> float | None:
        return {560: 24000.0, 585: 24050.0, 720: 24000.0, 920: 24100.0}.get(minute)

    rows = F.pair_needs("NIFTY", MON, atm, 50.0)
    got = {(t, k.opt, k.rel) for t, k, _, _ in rows}
    # 09:20 / 12:00 stays at the money: nothing to fetch
    assert got == {(3, "CE", -2), (3, "PE", -2), (4, "CE", -1), (4, "PE", -1),
                   (6, "CE", 1), (6, "PE", 1)}  # fmt: skip


def test_atm_at_takes_the_first_traded_minute_within_three() -> None:
    import numpy as np

    d = MON
    bars = {"ts": np.array([_epoch(d, 9, 22), _epoch(d, 9, 30)]),
            "strike": np.array([24000.0, 24050.0])}  # fmt: skip
    assert F.atm_at(bars, 560) == 24000.0
    assert F.atm_at(bars, 565) is None
    assert F.atm_at(None, 560) is None
