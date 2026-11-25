"""The first and last stored bar of a series without counting its bars.

`available_resolutions` counts every bar of a series (about 470,000 index entries on the
owner's store: 0.4 to 6 s), and `load_bars`, the Lab and the Simulator asked for it several
times for one run. `series_spans` gives the same first and last bar by index seeks and keeps
the answer until a bar is written (Simulator audit of 25 November 2026, findings F2 and SIM-04).
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import event

from onelot.clock import IST
from onelot.data import store
from onelot.data.store import available_resolutions, load_bars, save_bars, series_spans
from onelot.store import init_database


def frame(start: str, n: int, freq: str = "1min") -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp(start, tz=IST), periods=n, freq=freq)
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}, index=idx
    )


def test_spans_equal_the_counted_answer_without_its_count(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 's.db'}")
    try:
        save_bars(db.engine, "X", "1m", frame("2026-11-02 09:15", 375), "dhan")
        save_bars(db.engine, "X", "1d", frame("2026-10-01 09:15", 20, "1D"), "dhan")
        save_bars(db.engine, "Y", "5m", frame("2026-11-03 09:15", 75, "5min"), "dhan")
        for key in ("X", "Y", "NOBODY"):
            counted = available_resolutions(db.engine, key)
            assert series_spans(db.engine, key) == {r: v[:2] for r, v in counted.items()}
        assert set(series_spans(db.engine, "X")) == {"1m", "1d"}
        assert series_spans(db.engine, "NOBODY") == {}
    finally:
        db.dispose()


def test_spans_are_kept_and_dropped_when_a_bar_is_written(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 's.db'}")
    try:
        save_bars(db.engine, "X", "1m", frame("2026-11-02 09:15", 10), "dhan")
        first = series_spans(db.engine, "X")
        queries: list[str] = []
        event.listen(
            db.engine, "before_cursor_execute", lambda c, cur, stmt, *a: queries.append(stmt)
        )
        assert series_spans(db.engine, "X") == first  # the kept answer: no query at all
        assert queries == []
        save_bars(db.engine, "X", "1m", frame("2026-11-03 09:15", 10), "dhan")
        after = series_spans(db.engine, "X")
        assert after["1m"][0] == first["1m"][0]
        assert after["1m"][1] > first["1m"][1]  # the write dropped the kept answer
    finally:
        db.dispose()


def test_spans_grow_old(tmp_path, monkeypatch):
    db = init_database(f"sqlite:///{tmp_path / 's.db'}")
    try:
        save_bars(db.engine, "X", "1m", frame("2026-11-02 09:15", 10), "dhan")
        series_spans(db.engine, "X")
        queries: list[str] = []
        event.listen(
            db.engine, "before_cursor_execute", lambda c, cur, stmt, *a: queries.append(stmt)
        )
        monkeypatch.setattr(store, "SPANS_TTL_S", 0.0)  # another process may have written
        series_spans(db.engine, "X")
        assert queries, "an old answer is read again"
    finally:
        db.dispose()


def test_two_engines_never_share_an_answer(tmp_path):
    a = init_database(f"sqlite:///{tmp_path / 'a.db'}")
    b = init_database(f"sqlite:///{tmp_path / 'b.db'}")
    try:
        save_bars(a.engine, "X", "1m", frame("2026-11-02 09:15", 10), "dhan")
        assert "1m" in series_spans(a.engine, "X")
        assert series_spans(b.engine, "X") == {}
    finally:
        a.dispose()
        b.dispose()


def test_load_bars_never_counts(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 's.db'}")
    try:
        save_bars(db.engine, "X", "1m", frame("2026-11-02 09:15", 375), "dhan")
        queries: list[str] = []
        event.listen(
            db.engine, "before_cursor_execute", lambda c, cur, stmt, *a: queries.append(stmt)
        )
        out = load_bars(db.engine, "X", "5m")
        assert len(out) == 75
        assert not [q for q in queries if "count(" in q.lower()]
    finally:
        db.dispose()
