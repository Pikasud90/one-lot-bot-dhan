"""Known defects of the stored bar history, repaired when bars are loaded
(`data/hygiene.py`): mock sessions, two unadjusted demergers, and daily bars on another
price scale than the intraday bars."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from onelot.data import hygiene
from onelot.data.resample import resample_bars

IST = "Asia/Kolkata"


def minutes(day: str, px: float, n: int = 5) -> pd.DataFrame:
    """`n` one-minute bars from 09:15 of `day`, around `px` (the close rises 0.1 per bar)."""
    idx = pd.date_range(pd.Timestamp(f"{day} 09:15", tz=IST), periods=n, freq="1min")
    close = px + 0.1 * np.arange(n)
    return pd.DataFrame(
        {"open": close - 0.05, "high": close + 0.2, "low": close - 0.2, "close": close},
        index=idx,
    ).assign(volume=100.0)


def intraday(days: dict[str, float], n: int = 5) -> pd.DataFrame:
    return pd.concat([minutes(day, px, n) for day, px in days.items()])


def daily(days: dict[str, float], **extra) -> pd.DataFrame:
    """Stored daily bars, stamped 09:15 IST as the store keeps them."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{d} 09:15", tz=IST) for d in days])
    close = np.array(list(days.values()), dtype=float)
    out = pd.DataFrame(
        {"open": close - 1, "high": close + 2, "low": close - 2, "close": close}, index=idx
    ).assign(volume=1000.0)
    return out.assign(**extra)


# ── mock sessions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "junk"),
    [
        (dt.date(2022, 1, 8), True),  # a Saturday mock session
        (dt.date(2026, 3, 21), True),  # a Saturday mock session (SENSEX)
        (dt.date(2026, 11, 1), True),  # any other Sunday
        (dt.date(2021, 11, 4), True),  # the 2021 Muhurat weekday, stored as flat bars
        (dt.date(2024, 1, 20), False),  # the special Saturday session of 2024
        (dt.date(2025, 2, 1), False),  # a Budget Saturday
        (dt.date(2026, 11, 8), False),  # the Muhurat Sunday of 2026
        (dt.date(2026, 11, 6), False),  # an ordinary Friday
    ],
)
def test_a_weekend_is_real_only_when_it_is_a_known_special_session(day, junk):
    assert hygiene.is_junk_session(day) is junk


def test_mock_sessions_are_dropped_and_real_ones_kept():
    df = intraday(
        {
            "2022-01-07": 100.0,  # Friday
            "2022-01-08": 110.0,  # Saturday: a mock session, 10% away
            "2022-01-10": 101.0,  # Monday
            "2024-01-20": 102.0,  # the real Saturday session
        }
    )
    out = hygiene.drop_junk_sessions(df)
    assert sorted({ts.date() for ts in out.index}) == [
        dt.date(2022, 1, 7),
        dt.date(2022, 1, 10),
        dt.date(2024, 1, 20),
    ]
    assert len(out) == 15 and out["close"].max() < 105
    assert len(df) == 20  # the frame given is not changed


def test_the_day_is_the_exchanges_calendar_day_not_the_utc_one():
    """00:30 IST on a Saturday is Friday in UTC: it is still a Saturday bar."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2022-01-07 23:30", tz=IST), pd.Timestamp("2022-01-08 00:30", tz=IST)]
    )
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx)
    assert list(hygiene.drop_junk_sessions(df).index) == [idx[0]]


def test_a_frame_without_junk_is_returned_as_it_is():
    df = intraday({"2026-11-05": 100.0, "2026-11-06": 101.0})
    assert hygiene.drop_junk_sessions(df) is df
    empty = df.iloc[:0]
    assert hygiene.drop_junk_sessions(empty) is empty
    assert hygiene.drop_junk_sessions(None) is None


def test_daily_bars_lose_their_mock_days_too():
    df = daily({"2022-01-07": 100.0, "2022-01-08": 110.0, "2022-01-10": 101.0})
    assert [ts.date().isoformat() for ts in hygiene.drop_junk_sessions(df).index] == [
        "2022-01-07",
        "2022-01-10",
    ]


# ── unadjusted demergers ──────────────────────────────────────────────────────


def test_itc_prices_before_the_demerger_go_on_the_scale_after_it():
    df = intraday({"2025-01-03": 483.9, "2025-01-06": 455.6, "2025-01-07": 450.0})
    out = hygiene.back_adjust_intraday("ITC", df)
    factor = 455.6 / 483.9
    before = df.index < pd.Timestamp("2025-01-06", tz=IST)
    assert before.sum() == 5
    for col in hygiene.PRICE_COLUMNS:
        assert np.allclose(out.loc[before, col], df.loc[before, col] * factor)
        assert (out.loc[~before, col] == df.loc[~before, col]).all()
    assert (out["volume"] == df["volume"]).all()  # volume is left as the vendor sent it
    # the last close before the ex-date now meets the first open after it (was -5.8%)
    gap = out["open"].iloc[5] / out["close"].iloc[4] - 1
    assert abs(gap) < 0.002
    assert df["close"].iloc[0] == 483.9  # the frame given is not changed


def test_tata_motors_is_adjusted_by_its_own_factor():
    df = intraday({"2025-10-13": 664.9, "2025-10-14": 400.0})
    out = hygiene.back_adjust_intraday("TMPV", df)
    assert out["close"].iloc[0] == pytest.approx(664.9 * 400.0 / 664.9)
    assert out["close"].iloc[5] == pytest.approx(400.0)


def test_nothing_to_adjust_returns_the_frame_as_it_is():
    after = intraday({"2025-01-06": 455.6, "2025-01-07": 450.0})
    assert hygiene.back_adjust_intraday("ITC", after) is after  # all on the new scale already
    other = intraday({"2025-01-03": 2500.0})
    assert hygiene.back_adjust_intraday("RELIANCE", other) is other  # adjusted by the vendor
    assert hygiene.back_adjust_intraday("ITC", None) is None
    empty = after.iloc[:0]
    assert hygiene.back_adjust_intraday("ITC", empty) is empty


def test_clean_intraday_does_both_repairs():
    df = intraday(
        {"2025-01-03": 483.9, "2025-01-04": 600.0, "2025-01-06": 455.6}
    )  # 4th: a Saturday
    out = hygiene.clean_intraday("ITC", df)
    assert sorted({ts.date() for ts in out.index}) == [dt.date(2025, 1, 3), dt.date(2025, 1, 6)]
    assert out["close"].iloc[0] == pytest.approx(483.9 * 455.6 / 483.9)
    assert hygiene.clean_intraday("NIFTY", None) is None


# ── daily bars on the intraday price scale ────────────────────────────────────


def test_days_with_intraday_bars_are_derived_from_them():
    fine = intraday({"2026-11-04": 100.0, "2026-11-05": 102.0, "2026-11-06": 101.0})
    stored = daily({"2026-11-04": 130.0, "2026-11-05": 132.6, "2026-11-06": 131.3})
    out = hygiene.consistent_daily(stored, fine)
    derived = resample_bars(fine, "1d")
    pd.testing.assert_frame_equal(out, derived)
    assert out["close"].tolist() == pytest.approx([100.4, 102.4, 101.4])


def test_older_stored_days_are_kept_as_a_warm_up_on_the_intraday_scale():
    """The stored daily series runs 30% above the intraday bars (another vendor's index)."""
    fine = intraday({"2026-11-04": 100.0, "2026-11-05": 102.0, "2026-11-06": 101.0})
    derived = resample_bars(fine, "1d")
    stored = daily(
        {
            "2026-10-30": 126.1,
            "2026-11-02": 127.4,
            "2026-11-03": 128.7,
            "2026-11-04": 1.3 * 100.4,
            "2026-11-05": 1.3 * 102.4,
            "2026-11-06": 1.3 * 101.4,
        }
    )
    out = hygiene.consistent_daily(stored, fine)
    assert [ts.date().isoformat() for ts in out.index] == [
        "2026-10-30",
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
    ]
    assert out.index.is_monotonic_increasing and not out.index.duplicated().any()
    # one datetime index in the intraday bars' zone, whatever zone object the stored days had
    assert isinstance(out.index, pd.DatetimeIndex) and out.index.tz == derived.index.tz
    # the older days: divided back by the ratio of the two series (1.3)
    assert out["close"].iloc[:3].tolist() == pytest.approx([97.0, 98.0, 99.0])
    assert out["high"].iloc[0] == pytest.approx((126.1 + 2) / 1.3)
    assert out["low"].iloc[0] == pytest.approx((126.1 - 2) / 1.3)
    assert out["open"].iloc[0] == pytest.approx((126.1 - 1) / 1.3)
    assert out["volume"].iloc[0] == 1000.0  # volume is not a price
    # the days the intraday bars cover: exactly the derived ones
    pd.testing.assert_frame_equal(out.iloc[3:], derived[list(out.columns)], check_names=False)
    assert stored["close"].iloc[0] == 126.1  # the frame given is not changed


def test_the_scale_is_the_median_over_the_first_shared_sessions():
    """One wild shared day does not move the scale; days after the first 20 are not used."""
    days = pd.bdate_range("2026-09-01", periods=24)
    fine = intraday({d.date().isoformat(): 100.0 for d in days})
    stored_days = {"2026-08-31": 200.0} | {d.date().isoformat(): 200.8 for d in days}
    stored = daily(stored_days)
    stored.iloc[3, stored.columns.get_loc("close")] = 900.0  # a bad print among the first 20
    stored.iloc[21:, stored.columns.get_loc("close")] = 50.0  # past the 20 used: ignored
    out = hygiene.consistent_daily(stored, fine)
    assert hygiene.SCALE_SESSIONS == 20
    assert out["close"].iloc[0] == pytest.approx(200.0 * 100.4 / 200.8)


def test_without_an_overlap_the_older_days_are_kept_unscaled():
    fine = intraday({"2026-11-05": 100.0, "2026-11-06": 101.0})
    stored = daily({"2026-11-02": 97.0, "2026-11-03": 98.0})
    out = hygiene.consistent_daily(stored, fine)
    assert out["close"].tolist() == pytest.approx([97.0, 98.0, 100.4, 101.4])


def test_only_the_columns_both_series_have_are_kept():
    fine = intraday({"2026-11-05": 100.0})
    stored = daily({"2026-11-03": 98.0, "2026-11-05": 100.4}, oi=5.0)
    out = hygiene.consistent_daily(stored, fine)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_mock_days_in_the_stored_history_are_dropped():
    fine = intraday({"2022-01-10": 100.0})
    stored = daily({"2022-01-07": 99.0, "2022-01-08": 150.0, "2022-01-10": 100.4})
    out = hygiene.consistent_daily(stored, fine)
    assert [ts.date().isoformat() for ts in out.index] == ["2022-01-07", "2022-01-10"]


def test_without_intraday_bars_the_stored_days_stand():
    stored = daily({"2022-01-07": 99.0, "2022-01-08": 150.0, "2022-01-10": 100.0})
    for fine in (None, pd.DataFrame()):
        out = hygiene.consistent_daily(stored, fine)
        assert out["close"].tolist() == [99.0, 100.0]  # still without the mock day
    assert hygiene.consistent_daily(None, None) is None


def test_without_stored_days_the_derived_ones_stand():
    fine = intraday({"2026-11-05": 100.0, "2026-11-06": 101.0})
    derived = resample_bars(fine, "1d")
    for stored in (None, pd.DataFrame()):
        pd.testing.assert_frame_equal(hygiene.consistent_daily(stored, fine), derived)
