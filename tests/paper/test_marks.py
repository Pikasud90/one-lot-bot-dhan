"""US-023 AC3 (developer tests): an OPEN option trade is marked at every step that manages a bar.

    plan["position"]["mark_paise"] = to_paise(premium_at(option, last close, last close time))
                                     + plan["pricing"]["basis_paise"] (0 when there is none)

The Tester's acceptance tests (tests/acceptance/test_us023_ac3_option_repricing.py) cover the
close used, the basis, a failed mark and recovery. These pin the point-in-time pairing (the close
is priced at ITS close time, never the wall clock or the bar's open), the rounding (once,
HALF_EVEN, like every price at the boundary) and the floor (a mark is never zero or negative).

Worked option (conftest): BUY_CE BANKNIFTY, alert entry 45210.35, premium 312.34; the 10:06 bar
fills it and the 10:07 bar closes at 45225.00, so the last close time is 10:08.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from onelot.paper.money import to_paise
from onelot.store.schema import paper_trades

from .conftest import THU, ist, make_alert, table_rows

Row = tuple[float, float, float, float]
OPT_FILL = (45210.35, 45220.00, 45200.00, 45215.00)
QUIET = (45215.00, 45230.00, 45190.00, 45225.00)  # inside the 45110.35 stop / 45410.35 target


def bars(rows: Sequence[Row]) -> pd.DataFrame:
    idx = pd.date_range(ist(THU, 10, 6), periods=len(rows), freq="1min")
    o, h, l, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e3}, index=idx)


def feed(**frames: pd.DataFrame) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


def position(engine) -> dict[str, Any]:
    (row,) = table_rows(engine, paper_trades)
    assert row["status"] == "OPEN", row["status"]
    return dict(row["plan"]["position"])


class Recording:
    """premium_at: 312.34 at 45210.35, delta 0.65, floored at 0.05; records (underlying, ts)."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dt.datetime]] = []

    def fair(self, underlying: float) -> float:
        return max(0.05, 312.34 + 0.65 * (underlying - 45210.35))

    def __call__(self, option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float:
        self.calls.append((float(underlying), ts))
        return self.fair(underlying)


def test_the_last_close_is_priced_at_its_own_close_time(make_book, engine):
    pricer = Recording()
    book = make_book(premium_at=pricer)
    book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), feed(BANKNIFTY=bars([OPT_FILL, QUIET])))
    state = position(engine)
    assert state["last_close_paise"] == 4_522_500
    assert (45225.0, ist(THU, 10, 8)) in pricer.calls, pricer.calls  # the 10:07 bar's close time
    assert state["mark_paise"] == to_paise(pricer.fair(45225.0))
    assert type(state["mark_paise"]) is int


def test_a_mark_is_rounded_to_paise_once_half_even(make_book, engine):
    """A fair premium of 312.345 is 31234.5 paise: HALF_EVEN gives 31234 (HALF_UP would 31235)."""
    book = make_book(premium_at=lambda option, underlying, ts: 312.345)
    book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), feed(BANKNIFTY=bars([OPT_FILL, QUIET])))
    assert position(engine)["mark_paise"] == 31234


def test_a_negative_basis_never_marks_below_one_tick(make_book, engine):
    """PROPOSED (Dev Beta, for the Manager): a live-chain quote 12.34 below the model gives a
    basis of -1234 paise; once the model premium falls to 6.61 the sum is negative. A premium is
    never below one tick (the exit fill is floored there too), so the mark is 5 paise, never 0
    and never negative."""
    pricer = Recording()
    alert = make_alert("INDEX", stop=44000.0)  # a wide stop: the fall below is managed, not hit
    alert["economics"]["entry_price"] = 300.00  # the chain premium at the alert
    alert["economics"]["synthetic"] = False
    book = make_book(premium_at=pricer)
    book.on_alert(alert, ist(THU, 10, 5, 4))
    slump = (45210.00, 45215.00, 44700.00, 44740.00)  # closes at 44740: fair 6.6125
    book.step(ist(THU, 10, 8), feed(BANKNIFTY=bars([OPT_FILL, slump])))
    (row,) = table_rows(engine, paper_trades)
    assert row["plan"]["pricing"]["basis_paise"] == 30000 - 31234
    assert to_paise(pricer.fair(44740.0)) + row["plan"]["pricing"]["basis_paise"] < 0
    assert position(engine)["mark_paise"] == 5
