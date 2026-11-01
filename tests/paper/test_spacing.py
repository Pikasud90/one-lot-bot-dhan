"""Mixed bar spacing grades a trade delayed (US-001 AC9, Manager decision 6) — dev tests for T4.

    onelot.paper.fill.mixed_spacing(bars, *, base_min, session_date, now) -> bool

Mixed = two or more CONSECUTIVE gaps larger than the base spacing between bars of the
trade's IST session date that have closed by `now`. One hole (an outage) is not mixed; the
overnight gap is not mixed (only one date is looked at). The base is the trade's
resolution_min, or the inferred spacing when the alert did not say.

PaperBook.step regrades a live PENDING_FILL trade to delayed BEFORE it decides the fill (the
delayed window then applies), persists it, and never upgrades a trade back to live.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from onelot.paper.fill import normalise_bars
from onelot.store.schema import paper_trades

from .conftest import THU, ist, make_alert, make_bars, table_rows

WED = THU - dt.timedelta(days=1)


def at_minutes(day: dt.date, hh: int, minutes: list[int]) -> pd.DataFrame:
    """1000.0 bars opening at hh:mm for each mm in `minutes` (mm may pass 59)."""
    stamps = [ist(day, hh, 0) + dt.timedelta(minutes=m) for m in minutes]
    o = np.full(len(stamps), 1000.0)
    return pd.DataFrame(
        {"open": o, "high": o + 1, "low": o - 1, "close": o, "volume": 1000.0},
        index=pd.DatetimeIndex(stamps),
    )


def is_mixed(frame: pd.DataFrame, base: int = 1, day: dt.date = THU, now=None) -> bool:
    from onelot.paper.fill import mixed_spacing

    view = normalise_bars(frame)
    assert view is not None
    return mixed_spacing(view, base_min=base, session_date=day, now=now or ist(day, 12, 0))


# ── the pure rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("minutes", "base", "mixed"),
    [
        (list(range(0, 10)), 1, False),  # uniform 1m
        (list(range(0, 20, 2)), 2, False),  # uniform 2m on a 2m feed
        (list(range(0, 20, 2)), 1, True),  # 2m bars on a feed that said 1m
        ([0, 1, 2, 3, 4, 10, 11, 12, 13], 1, False),  # one hole: an outage, not mixed
        ([5, 6, 7, 8, 10, 12, 14], 1, True),  # 1m then 2m: the acceptance frame
        ([0, 1, 3, 4, 6, 7], 1, False),  # two larger gaps, never consecutive
        ([0, 2, 4, 5, 6], 1, True),  # two consecutive at the start
        ([0, 1, 2, 4, 6], 1, True),  # two consecutive at the end
    ],
)
def test_two_consecutive_gaps_larger_than_the_base_are_mixed(minutes, base, mixed):
    assert is_mixed(at_minutes(THU, 10, minutes), base=base) is mixed


def test_the_overnight_gap_is_not_mixed():
    frame = pd.concat([at_minutes(WED, 15, list(range(20, 30))), at_minutes(THU, 9, [15, 16, 17])])
    assert is_mixed(frame) is False


def test_only_the_trades_session_date_is_looked_at():
    wed_two_minute = at_minutes(WED, 10, list(range(0, 20, 2)))
    thu_one_minute = at_minutes(THU, 10, list(range(0, 10)))
    frame = pd.concat([wed_two_minute, thu_one_minute])
    assert is_mixed(frame, day=THU) is False
    assert is_mixed(frame, day=WED, now=ist(WED, 12, 0)) is True


def test_bars_that_have_not_closed_by_now_are_not_looked_at():
    frame = at_minutes(THU, 10, [*range(0, 10), 11, 13, 15])
    assert is_mixed(frame, now=ist(THU, 10, 10)) is False
    assert is_mixed(frame, now=ist(THU, 10, 30)) is True


def test_too_few_bars_are_not_mixed():
    assert is_mixed(at_minutes(THU, 10, [0])) is False
    assert is_mixed(at_minutes(THU, 10, [0, 5])) is False


# ── the book ─────────────────────────────────────────────────────────────────────


def series(**frames):
    return lambda key: frames.get(key)


def mixed_frame() -> pd.DataFrame:
    return at_minutes(THU, 10, [5, 6, 7, 8, 10, 12, 14])


def test_a_live_trade_on_a_mixed_frame_is_regraded_delayed_and_it_sticks(make_book, engine):
    book = make_book()
    trade = book.on_alert(make_alert(source="dhan"), ist(THU, 10, 5, 4))
    assert trade is not None and trade.grade == "live"
    early = at_minutes(THU, 9, [50, 51, 52, 53, 54, 56, 58, 60])  # 1m to 9:54, then 2m to 10:00

    book.step(ist(THU, 10, 5, 30), series(RELIANCE=early))  # nothing to fill yet

    (row,) = table_rows(engine, paper_trades)
    assert (row["grade"], row["status"]) == ("delayed", "PENDING_FILL")
    assert [t.grade for t in book.open_trades()] == ["delayed"]
    assert [t.grade for t in make_book().open_trades()] == ["delayed"]  # reloaded from the DB
    uniform = make_bars(ist(THU, 9, 50), [1000.0] * 15)  # 9:50 .. 10:04, all 1m
    book.step(ist(THU, 10, 5, 40), series(RELIANCE=uniform))
    (row,) = table_rows(engine, paper_trades)
    assert (row["grade"], row["status"]) == ("delayed", "PENDING_FILL")  # never upgraded


def test_the_acceptance_mixed_frame_regrades_and_still_fills_at_the_1006_open(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(source="dhan"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 20), series(RELIANCE=mixed_frame()))
    (row,) = table_rows(engine, paper_trades)
    assert (row["grade"], row["status"], row["entry_paise"]) == ("delayed", "OPEN", 100030)


def test_the_regrade_comes_before_the_fill_decision(make_book, engine):
    """Live window ends 10:15:03; the feed turns into 2m bars from 10:16. Regraded first, the
    delayed window (+15 min) makes the 10:16 bar the fill instead of an 'expired' NO_FILL."""
    book = make_book()
    book.on_alert(make_alert(source="dhan"), ist(THU, 10, 5, 4))
    frame = at_minutes(THU, 10, [0, 1, 2, 3, 4, 16, 18, 20])

    book.step(ist(THU, 10, 30), series(RELIANCE=frame))

    (row,) = table_rows(engine, paper_trades)
    assert (row["grade"], row["status"]) == ("delayed", "OPEN")
    assert (row["fill_bar_ts"], row["entry_paise"]) == (ist(THU, 10, 16), 100030)


def test_a_uniform_frame_leaves_the_row_untouched(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(source="dhan"), ist(THU, 10, 5, 4))
    before = table_rows(engine, paper_trades)
    frame = make_bars(ist(THU, 10, 0), [1000.0] * 5)  # 10:00 .. 10:04, nothing to fill yet
    assert book.step(ist(THU, 10, 5, 30), series(RELIANCE=frame)) == []
    assert table_rows(engine, paper_trades) == before


def test_without_a_resolution_on_the_alert_the_inferred_spacing_is_the_base(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(source="dhan", resolution=""), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 20), series(RELIANCE=make_bars(ist(THU, 10, 0), [1e3] * 8, res_min=2)))
    (row,) = table_rows(engine, paper_trades)
    assert row["grade"] == "live" and row["status"] == "OPEN"
