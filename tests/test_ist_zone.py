"""US-096: the app's IST is ONE fixed +05:30 offset, and it stays fast.

`ZoneInfo("Asia/Kolkata")` made pandas call Python once per stamp without releasing the
interpreter lock (36,000 one-minute stamps: normalize() 2.35 s), which froze the whole app.
These tests fail if a second zone object for India comes back under src/onelot/ or scripts/,
or if the one `IST` stops being fast.
"""

from __future__ import annotations

import datetime as dt
import io
import time
import tokenize
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from onelot.clock import IST, as_ist, now_ist, to_ist

ROOT = Path(__file__).resolve().parents[1]
ZONE_NAMES = {"Asia/Kolkata", "Asia/Calcutta"}
#: the one place a bare zone name may stay: a label in the session config that nothing reads
#: and nothing hands to pandas or datetime (config/config.yaml carries the same word)
ALLOWED = {("src/onelot/config.py", 'timezone: str = "Asia/Kolkata"')}


def _python_files() -> list[Path]:
    return sorted([*(ROOT / "src" / "onelot").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")])


def _offences(path: Path, rel: str | None = None) -> list[str]:
    """Code (not comments or prose) that names India's zone or builds a zoneinfo/pytz zone."""
    rel = rel or path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type not in (tokenize.STRING, tokenize.NAME):
            continue
        line = lines[tok.start[0] - 1].strip()
        if tok.type == tokenize.STRING:
            try:
                value = eval(tok.string, {}, {})  # noqa: S307 - a literal from our own source
            except Exception:  # noqa: BLE001 - f-strings and the like: not a bare zone name
                continue
            if isinstance(value, str) and value.strip() in ZONE_NAMES:
                if (rel, line) not in ALLOWED:
                    out.append(f"{rel}:{tok.start[0]}: {line}")
        elif tok.type == tokenize.NAME and tok.string in {"ZoneInfo", "zoneinfo", "pytz"}:
            out.append(f"{rel}:{tok.start[0]}: {line}")
    return out


def test_no_second_zone_object_for_india_in_the_app_or_the_scripts():
    found = [o for p in _python_files() for o in _offences(p)]
    assert found == [], "use onelot.clock.IST (US-096):\n" + "\n".join(found)


def test_the_guard_catches_what_it_is_for(tmp_path):
    """The scan itself works: each of these lines is caught, a comment is not."""
    bad = [
        'IST = ZoneInfo("Asia/Kolkata")',
        'idx = idx.tz_convert("Asia/Kolkata")',
        "t = pd.Timestamp(x, tz='Asia/Calcutta')",
        "import pytz",
    ]
    for i, line in enumerate(bad):
        f = tmp_path / f"probe_{i}.py"
        f.write_text(f"# a comment naming Asia/Kolkata is fine\n{line}\n")
        found = _offences(f, rel=f"scripts/probe_{i}.py")
        assert found and all(":2:" in x for x in found), line


def test_ist_is_a_fixed_five_thirty_that_prints_as_before():
    assert IST.utcoffset(None) == dt.timedelta(hours=5, minutes=30)
    assert IST.dst(None) is None
    t = dt.datetime(2026, 11, 25, 9, 15, tzinfo=IST)
    assert t.isoformat() == "2026-11-25T09:15:00+05:30"
    assert t.strftime("%H:%M %Z") == "09:15 IST"
    # the same instants as the zone database's India, for every year the app holds
    tzdb = ZoneInfo("Asia/Kolkata")  # the comparison itself: tests may name the zone
    for year in range(2005, 2031):
        for month in (1, 7):
            local = dt.datetime(year, month, 1, 9, 15)
            assert local.replace(tzinfo=IST) == local.replace(tzinfo=tzdb)
    assert now_ist().utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert to_ist(dt.datetime(2026, 1, 1, 3, 45, tzinfo=dt.UTC)).hour == 9


def test_pandas_keeps_ist_as_a_fixed_offset_and_stores_the_same_instants():
    idx = pd.date_range("2026-11-02 09:15", periods=375, freq="min", tz=IST)
    assert idx.tz == IST and str(idx.dtype) == "datetime64[ns, IST]"
    old = pd.date_range("2026-11-02 09:15", periods=375, freq="min", tz="Asia/Kolkata")
    assert (idx.asi8 == old.asi8).all()  # the store keeps naive UTC: the same numbers
    assert str(idx[0]) == "2026-11-02 09:15:00+05:30"


def test_an_old_cached_frame_joins_a_new_one_without_turning_into_objects():
    old_zone = pd.date_range("2026-11-02 09:15", periods=3, freq="min", tz="Asia/Kolkata")
    new = pd.DataFrame({"close": [4.0]}, index=pd.date_range("2026-11-02 09:18", periods=1,
                                                              freq="min", tz=IST))  # fmt: skip
    old = pd.DataFrame({"close": [1.0, 2.0, 3.0], "at": old_zone}, index=old_zone)
    raw = pd.concat([old[["close"]], new])
    assert raw.index.dtype == object  # what as_ist is for
    fixed = as_ist({"bars": [old]})["bars"][0]
    assert fixed.index.tz == IST and fixed["at"].dt.tz == IST
    joined = pd.concat([fixed[["close"]], new])
    assert str(joined.index.dtype) == "datetime64[ns, IST]"
    assert (joined.index.asi8[:3] == old_zone.asi8).all()
    utc = pd.Series([1], index=old_zone[:1].tz_convert("UTC"))
    assert as_ist(utc).index.tz == dt.UTC or str(as_ist(utc).index.tz) == "UTC"  # untouched


def _best_of_five(fn) -> float:
    best = float("inf")
    for _ in range(5):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


@pytest.mark.parametrize(
    ("op", "limit"), [("normalize", 0.05), ("tz_convert", 0.05), ("hour", 0.05), ("time", 0.5)]
)
def test_a_36000_stamp_ist_index_is_fast(op, limit):
    """The stall US-096 removed (best of five). With ZoneInfo each of these took seconds on
    36,000 stamps; now normalize, tz_convert and hour take about a millisecond, and `.time`
    (36,000 Python objects built) a few tens of milliseconds."""
    utc = pd.date_range("2026-01-01 03:45", periods=36_000, freq="min", tz="UTC")
    idx = utc.tz_convert(IST)
    fns = {
        "normalize": idx.normalize,
        "tz_convert": lambda: utc.tz_convert(IST).normalize(),
        "hour": lambda: idx.hour,
        "time": lambda: idx.time,
    }
    assert _best_of_five(fns[op]) < limit
