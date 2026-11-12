"""The stored days the desk reads once a day for the trend ladder's daily timeframe
(`desk/history.py`): the store's daily bars, with every day the stored one-minute bars
cover derived from those instead, all on the intraday price scale and without mock
sessions. Read-only: nothing here writes to the store but the tests' own set-up."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.data.hygiene import clean_intraday, consistent_daily
from onelot.data.resample import resample_bars
from onelot.data.store import save_bars
from onelot.desk.history import DAILY_DAYS, FINE_DAYS, FINE_RESOLUTION, daily_history
from onelot.store import init_database

IST = "Asia/Kolkata"


def at(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=IST)


@pytest.fixture
def engine(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'history.db'}")
    yield db.engine
    db.dispose()


def minute_bars(start: str, days: int = 1, seed: int = 7, base: float = 1000.0) -> pd.DataFrame:
    """One-minute bars of `days` weekday sessions from `start`."""
    return make_intraday(days=days, seed=seed, freq_min=1, start=start, base=base, vol=0.0003)


def day_bars(dates, close, volume: float = 5e5) -> pd.DataFrame:
    """Daily bars the way the store keeps them: stamped 09:15 IST."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15) for d in dates],
        name="ts",
    )
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def dates_of(frame: pd.DataFrame) -> list[str]:
    return [ts.date().isoformat() for ts in frame.index]


def same(left: pd.DataFrame, right: pd.DataFrame) -> None:
    """The same bars at the same moments of exchange time. (The store hands back the
    standard library's zone object and a test frame carries pytz's: the same zone, but two
    index types to pandas, so the moments are compared in UTC.)"""
    assert str(left.index.tz) == str(right.index.tz) == IST
    pd.testing.assert_frame_equal(
        left.tz_convert("UTC"), right.tz_convert("UTC"), check_names=False, check_freq=False
    )


NOW = at("2026-11-09 08:00")  # a Monday, before the open


def test_the_windows_are_the_ones_the_ladder_was_sized_for():
    """About 110 sessions of one-minute bars (well past the 70 a daily Supertrend(14) needs)
    and more than a year of daily bars before them."""
    assert (FINE_DAYS, DAILY_DAYS, FINE_RESOLUTION) == (160, 420, "1m")


def test_stored_days_and_minute_bars_make_one_daily_series(engine):
    """The stored daily bars run 30% above the one-minute bars (another vendor's index, as in
    the real store): the days with minute bars are derived from them, the older days are
    kept as a warm-up and brought onto the minute bars' scale."""
    fine = minute_bars("2026-11-04", days=3)  # Wed 4, Thu 5, Fri 6 November
    derived = resample_bars(fine, "1d")
    older = pd.bdate_range("2026-10-28", "2026-11-03")  # five weekdays before them
    stored = day_bars(
        list(older) + [ts.date() for ts in derived.index],
        np.r_[1.3 * np.array([990.0, 992.0, 994.0, 996.0, 998.0]), 1.3 * derived["close"].values],
    )
    save_bars(engine, "NIFTY", "1m", fine, "dhan")
    save_bars(engine, "NIFTY", "1d", stored, "yahoo")

    out = daily_history(engine, "NIFTY", NOW.to_pydatetime())

    assert dates_of(out) == [d.date().isoformat() for d in older] + dates_of(derived)
    same(out.iloc[5:], derived[list(out.columns)])
    assert out["close"].iloc[:5].tolist() == pytest.approx([990.0, 992.0, 994.0, 996.0, 998.0])
    assert out["high"].iloc[0] == pytest.approx(990.0 * 1.004)
    assert out["volume"].iloc[0] == 5e5  # volume is not a price
    assert out.index.is_monotonic_increasing and not out.index.duplicated().any()
    assert str(out.index.tz) == IST
    # one rule for the desk and the Lab: the series is the Lab's, built from the same bars
    same(out, consistent_daily(stored, clean_intraday("NIFTY", fine)))


def test_nothing_stored_is_an_empty_frame(engine):
    out = daily_history(engine, "NIFTY", NOW.to_pydatetime())
    assert isinstance(out, pd.DataFrame) and out.empty


def test_another_instruments_bars_are_not_read(engine):
    save_bars(engine, "BANKNIFTY", "1m", minute_bars("2026-11-06"), "dhan")
    save_bars(engine, "BANKNIFTY", "1d", day_bars(["2026-11-05"], [1000.0]), "dhan")
    assert daily_history(engine, "NIFTY", NOW.to_pydatetime()).empty
    assert dates_of(daily_history(engine, "BANKNIFTY", NOW.to_pydatetime())) == [
        "2026-11-05",
        "2026-11-06",
    ]


def test_with_daily_bars_only_the_stored_days_stand(engine):
    stored = day_bars(pd.bdate_range("2026-10-26", "2026-11-06"), np.linspace(1000, 1018, 10))
    save_bars(engine, "NIFTY", "1d", stored, "dhan")
    same(daily_history(engine, "NIFTY", NOW.to_pydatetime()), stored)


def test_with_minute_bars_only_the_days_are_derived_from_them(engine):
    fine = minute_bars("2026-11-04", days=3)
    save_bars(engine, "NIFTY", "1m", fine, "dhan")
    same(daily_history(engine, "NIFTY", NOW.to_pydatetime()), resample_bars(fine, "1d"))


def test_other_stored_resolutions_are_not_used(engine):
    """The days come from the one-minute bars only: five-minute bars in the store (an older
    download) neither add a day nor change one."""
    save_bars(engine, "NIFTY", "5m", make_intraday(days=3, start="2026-11-04"), "dhan")
    assert daily_history(engine, "NIFTY", NOW.to_pydatetime()).empty


def test_bars_after_the_moment_asked_for_are_not_read(engine):
    """Read in the middle of Thursday's session: Friday is not there, and Thursday is the
    day so far (an unfinished day: the ladder reads a day only once it has closed)."""
    fine = minute_bars("2026-11-04", days=3)
    save_bars(engine, "NIFTY", "1m", fine, "dhan")
    save_bars(
        engine,
        "NIFTY",
        "1d",
        day_bars(["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"], [1.0, 2.0, 3.0, 4.0]),
        "dhan",
    )
    moment = at("2026-11-05 10:40")

    out = daily_history(engine, "NIFTY", moment.to_pydatetime())

    assert dates_of(out) == ["2026-11-03", "2026-11-04", "2026-11-05"]
    so_far = fine[(fine.index >= at("2026-11-05")) & (fine.index < moment)]
    assert len(so_far) == 85  # 09:15 .. 10:39
    today = out.iloc[-1]
    assert today["open"] == so_far["open"].iloc[0]
    assert today["close"] == so_far["close"].iloc[-1]
    assert today["high"] == so_far["high"].max() and today["low"] == so_far["low"].min()
    assert today["volume"] == pytest.approx(so_far["volume"].sum())
    # the bar that opens at the moment itself is not read either
    assert out.iloc[-1]["close"] != fine.loc[moment, "close"]


def test_minute_bars_older_than_the_fine_window_are_left_to_the_daily_bars(engine):
    """Monday 2 March is 252 days back: outside the 160 days of one-minute bars, so its day
    is the stored daily bar. Asked for a longer window, it is derived from the minutes."""
    old, recent = minute_bars("2026-03-02", seed=3), minute_bars("2026-11-06", seed=4)
    save_bars(engine, "NIFTY", "1m", pd.concat([old, recent]), "dhan")
    recent_day = resample_bars(recent, "1d")
    stored = day_bars(["2026-03-02", "2026-11-06"], [987.0, float(recent_day["close"].iloc[0])])
    save_bars(engine, "NIFTY", "1d", stored, "dhan")

    out = daily_history(engine, "NIFTY", NOW.to_pydatetime())
    assert dates_of(out) == ["2026-03-02", "2026-11-06"]
    assert out["close"].iloc[0] == pytest.approx(987.0) and out["volume"].iloc[0] == 5e5
    same(out.iloc[1:], recent_day[list(out.columns)])

    longer = daily_history(engine, "NIFTY", NOW.to_pydatetime(), fine_days=300)
    same(longer, resample_bars(pd.concat([old, recent]), "1d"))


def test_the_fine_window_is_counted_back_from_the_moment_asked_for(engine):
    """160 days before Monday 9 November 08:00 is Tuesday 2 June 08:00: that day's session is
    read whole, Monday 1 June's is not."""
    assert NOW - pd.Timedelta(days=FINE_DAYS) == at("2026-06-02 08:00")
    save_bars(engine, "NIFTY", "1m", minute_bars("2026-06-01", days=2), "dhan")
    assert dates_of(daily_history(engine, "NIFTY", NOW.to_pydatetime())) == ["2026-06-02"]


def test_daily_bars_older_than_the_daily_window_are_not_read(engine):
    """420 days before Monday 9 November 2026 08:00 is Monday 15 September 2025 08:00."""
    assert NOW - pd.Timedelta(days=DAILY_DAYS) == at("2025-09-15 08:00")
    stored = day_bars(["2025-09-11", "2025-09-12", "2025-09-15", "2026-11-06"], [1, 2, 3, 4])
    save_bars(engine, "NIFTY", "1d", stored, "dhan")
    assert dates_of(daily_history(engine, "NIFTY", NOW.to_pydatetime())) == [
        "2025-09-15",
        "2026-11-06",
    ]


def test_a_mock_session_is_in_neither_the_minutes_nor_the_days(engine):
    """Saturday 10 October 2026 is a mock session: the store holds its bars (at twice the
    price here), the history does not."""
    fine = minute_bars("2026-10-09", days=2)  # Fri 9, Mon 12 October
    friday = fine[fine.index < at("2026-10-10")]
    mock = friday.copy()
    mock.index = mock.index + pd.Timedelta(days=1)
    mock[["open", "high", "low", "close"]] *= 2.0
    save_bars(engine, "NIFTY", "1m", pd.concat([fine, mock]).sort_index(), "dhan")
    save_bars(
        engine,
        "NIFTY",
        "1d",
        day_bars(["2026-10-08", "2026-10-10"], [float(friday["close"].iloc[0]), 2000.0]),
        "dhan",
    )

    out = daily_history(engine, "NIFTY", at("2026-10-13 08:00").to_pydatetime())

    assert dates_of(out) == ["2026-10-08", "2026-10-09", "2026-10-12"]
    assert out["high"].max() < 1100.0
    same(out.iloc[1:], resample_bars(fine, "1d")[list(out.columns)])


def test_an_unadjusted_demerger_is_repaired_before_the_days_are_built(engine):
    """ITC's stored one-minute bars before 6 January 2025 are on the price scale from before
    the hotels demerger; the days are built on the scale after it. Another share with the
    very same bars is left alone."""
    before = minute_bars("2025-01-03", base=483.9)
    after = minute_bars("2025-01-06", seed=8, base=455.6)
    fine = pd.concat([before, after])
    for key in ("ITC", "RELIANCE"):
        save_bars(engine, key, "1m", fine, "dhan")
    moment = at("2025-01-07 08:00").to_pydatetime()
    raw = resample_bars(fine, "1d")

    itc = daily_history(engine, "ITC", moment)
    assert dates_of(itc) == ["2025-01-03", "2025-01-06"]
    factor = 455.6 / 483.9
    for column in ("open", "high", "low", "close"):
        assert itc[column].iloc[0] == pytest.approx(raw[column].iloc[0] * factor)
        assert itc[column].iloc[1] == pytest.approx(raw[column].iloc[1])
    assert itc["volume"].tolist() == pytest.approx(raw["volume"].tolist())

    same(daily_history(engine, "RELIANCE", moment), raw)


def test_a_naive_moment_is_read_as_exchange_time(engine):
    fine = minute_bars("2026-11-04", days=3)
    save_bars(engine, "NIFTY", "1m", fine, "dhan")
    moment = at("2026-11-05 10:40")
    aware = daily_history(engine, "NIFTY", moment.to_pydatetime())
    naive = daily_history(engine, "NIFTY", moment.tz_localize(None).to_pydatetime())
    same(naive, aware)
    assert dates_of(aware) == ["2026-11-04", "2026-11-05"]


def test_reading_the_history_writes_nothing(engine):
    from sqlalchemy import func, select

    from onelot.data.schema import bars

    fine = minute_bars("2026-11-06")
    save_bars(engine, "NIFTY", "1m", fine, "dhan")
    save_bars(engine, "NIFTY", "1d", day_bars(["2026-11-05"], [1000.0]), "dhan")

    def stored_rows() -> int:
        with engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(bars)).scalar_one())

    count = stored_rows()
    assert count == len(fine) + 1
    for _ in range(2):
        assert len(daily_history(engine, "NIFTY", NOW.to_pydatetime())) == 2
    assert stored_rows() == count
