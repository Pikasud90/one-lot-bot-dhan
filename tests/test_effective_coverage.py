"""Owner feedback OF-3: what the Lab can load per timeframe, from the rows stored.

`effective_coverage(coverage_report rows)` mirrors `load_bars`: a timeframe is built from
every stored resolution that can produce it, so its history is the widest base's.
"""

from __future__ import annotations

from onelot.data.store import effective_coverage


def row(key, res, source, first, last, bars=100):
    return {
        "series_key": key,
        "resolution": res,
        "source": source,
        "first": first,
        "last": last,
        "bars": bars,
    }


def by(rows):
    return {(r["series_key"], r["timeframe"]): r for r in rows}


def test_fifteen_minutes_from_five_years_of_dhan_one_minute_bars_is_five_years():
    got = by(
        effective_coverage(
            [
                row("HDFCBANK", "1m", "dhan", "2021-11-01", "2026-11-02 10:25"),
                row("HDFCBANK", "15m", "yahoo", "2026-08-31", "2026-10-27 15:15"),
            ]
        )
    )
    r = got[("HDFCBANK", "15m")]
    assert (r["first"], r["base"], r["source"]) == ("2021-11-01", "1m", "dhan")
    assert r["days"] > 1800 and r["sources"] == ["dhan", "yahoo"]


def test_a_yahoo_only_series_stays_at_its_sixty_days_and_sixty_minutes_uses_its_two_years():
    got = by(
        effective_coverage(
            [
                row("USDINR", "5m", "yahoo", "2026-08-31", "2026-10-27 15:45"),
                row("USDINR", "60m", "yahoo", "2024-10-28", "2026-10-27 15:00"),
                row("USDINR", "1d", "yahoo", "2017-01-02", "2026-10-27 05:30"),
            ]
        )
    )
    assert got[("USDINR", "15m")]["days"] == 57 and got[("USDINR", "15m")]["base"] == "5m"
    assert got[("USDINR", "30m")]["base"] == "5m"
    assert got[("USDINR", "60m")]["base"] == "60m" and got[("USDINR", "60m")]["days"] == 729
    assert got[("USDINR", "1d")]["first"] == "2017-01-02"


def test_a_series_with_daily_bars_only_has_no_intraday_rows():
    got = by(effective_coverage([row("MM", "1d", "dhan", "2017-01-02", "2026-10-30 09:15")]))
    assert set(got) == {("MM", "1d")}


def test_the_last_bar_is_the_latest_of_any_base():
    got = by(
        effective_coverage(
            [
                row("X", "1m", "dhan", "2021-11-01", "2026-11-02 10:25"),
                row("X", "5m", "yahoo", "2026-08-31", "2026-11-06 15:25"),
            ]
        )
    )
    assert got[("X", "15m")]["last"] == "2026-11-06 15:25"
    assert got[("X", "15m")]["first"] == "2021-11-01"


def test_the_coverage_scan_is_cached_until_a_bar_is_written(tmp_path, monkeypatch):
    """The scan costs seconds on a large corpus: asked twice, it runs once; a bar written
    (save_bars) makes the next answer fresh."""
    import pandas as pd

    from onelot.clock import IST
    from onelot.data import store
    from onelot.store import init_database

    db = init_database(f"sqlite:///{tmp_path / 'cov.db'}")
    try:
        calls = []
        real = store._coverage_scan
        monkeypatch.setattr(store, "_coverage_scan", lambda e: calls.append(1) or real(e))
        idx = pd.DatetimeIndex([pd.Timestamp("2026-11-02 09:15", tz=IST)])
        bar = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}, index=idx)
        store.save_bars(db.engine, "X", "5m", bar, "yahoo")
        first = store.coverage_report(db.engine)
        second = store.coverage_report(db.engine)
        assert len(calls) == 1 and first.equals(second)
        store.save_bars(db.engine, "Y", "5m", bar, "dhan")
        third = store.coverage_report(db.engine)
        assert len(calls) == 2 and set(third["series_key"]) == {"X", "Y"}
    finally:
        db.dispose()
