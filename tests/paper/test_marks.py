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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pandas as pd
import pytest

from onelot.paper.money import to_paise
from onelot.store.schema import paper_events, paper_trades

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


# ── Manager decision (1), option A: the live-chain basis prices the fills too ──────────────
def ask_paise(premium: str, spread: str = "0.006", ticks: int = 2) -> int:
    """Oracle: ceil_tick(premium x (1 + spread) + ticks x 0.05), in paise, in Decimal."""
    raw = Decimal(premium) * (1 + Decimal(spread)) + ticks * Decimal("0.05")
    return int((raw / Decimal("0.05")).to_integral_value(rounding=ROUND_CEILING)) * 5


def bid_paise(premium: str, spread: str = "0.006", ticks: int = 2) -> int:
    """Oracle: floor_tick(premium x (1 - spread) - ticks x 0.05), at least one tick, in paise."""
    raw = Decimal(premium) * (1 - Decimal(spread)) - ticks * Decimal("0.05")
    return max(int((raw / Decimal("0.05")).to_integral_value(rounding=ROUND_FLOOR)) * 5, 5)


def chain_alert(chain: float = 300.00) -> dict[str, Any]:
    """The worked option quoted on the live chain at `chain` (the model says 312.34)."""
    alert = make_alert("INDEX")
    alert["economics"]["entry_price"] = chain
    alert["economics"]["synthetic"] = False
    return alert


def test_a_live_chain_entry_and_its_risk_leg_are_priced_at_model_plus_basis(make_book, engine):
    """Basis 300.00 - 312.34 = -12.34. Entry: the model 312.34 at the 45210.35 open, plus the
    basis, is 300.00, so the ask is 301.90 (not 314.35). The stop leg: 247.34 at 45110.35,
    plus the basis, is 235.00, sold at 233.45. Risk (301.90 - 233.45) x 30 = 2,053.50."""
    book = make_book(premium_at=Recording())
    book.on_alert(chain_alert(), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 7), feed(BANKNIFTY=bars([OPT_FILL])))
    (row,) = table_rows(engine, paper_trades)
    assert row["plan"]["pricing"]["basis_paise"] == -1234
    assert row["entry_paise"] == ask_paise("300.00") == 30190
    assert row["risk_paise"] == (30190 - bid_paise("235.00")) * 30 == 205350


def test_a_live_chain_exit_is_sold_at_model_plus_basis(make_book, engine):
    """The target 45410.35 trades through: the model there is 442.34, plus the basis 430.00,
    sold at floor_tick(430.00 x 0.994 - 0.10) = 427.30."""
    book = make_book(premium_at=Recording())
    book.on_alert(chain_alert(), ist(THU, 10, 5, 4))
    through = (45215.00, 45420.00, 45200.00, 45400.00)
    book.step(ist(THU, 10, 8), feed(BANKNIFTY=bars([OPT_FILL, through])))
    (row,) = table_rows(engine, paper_trades)
    assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET")
    assert row["exit_paise"] == bid_paise("430.00") == 42730
    assert row["gross_paise"] == (42730 - 30190) * 30


def test_a_synthetic_alert_is_priced_on_the_model_alone(make_book, engine):
    """No basis recorded: the ask is the model's own, 312.34 -> 314.35 (the Lab's number)."""
    book = make_book(premium_at=Recording())
    book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 7), feed(BANKNIFTY=bars([OPT_FILL])))
    (row,) = table_rows(engine, paper_trades)
    assert "basis_paise" not in row["plan"]["pricing"]
    assert row["entry_paise"] == ask_paise("312.34") == 31435


def test_a_step_that_manages_no_usable_close_keeps_the_mark_and_prices_nothing(make_book, engine):
    """A bar whose prices are NaN moves the cursor but not the last close: the mark stays the
    one priced at that close, nothing is repriced and no PRICING_UNAVAILABLE is booked."""
    pricer = Recording()
    book = make_book(premium_at=pricer)
    book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
    nan = float("nan")
    frame = bars([OPT_FILL, QUIET, (nan, nan, nan, nan)])
    book.step(ist(THU, 10, 8), feed(BANKNIFTY=frame))
    marked = position(engine)
    asked = len(pricer.calls)
    book.step(ist(THU, 10, 9), feed(BANKNIFTY=frame))
    state = position(engine)
    assert state["last_bar_ts"] != marked["last_bar_ts"], "the NaN bar was managed"
    assert state["mark_paise"] == marked["mark_paise"] == to_paise(pricer.fair(45225.0))
    assert len(pricer.calls) == asked
    kinds = [r["kind"] for r in table_rows(engine, paper_events)]
    assert "PRICING_UNAVAILABLE" not in kinds


@pytest.mark.parametrize("stored", [312.5, True, "31234", None])
def test_the_panel_shows_no_mark_for_a_stored_mark_that_is_not_integer_paise(
    make_book, engine, stored
):
    """A mark that is not a plain int in the plan (a corrupt write, or a failed mark) is JSON
    null, mark and unrealised alike: never truncated, never 0."""
    book = make_book(premium_at=Recording())
    book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), feed(BANKNIFTY=bars([OPT_FILL, QUIET])))
    (row,) = table_rows(engine, paper_trades)
    plan = row["plan"]
    plan["position"]["mark_paise"] = stored
    with engine.begin() as conn:
        conn.execute(paper_trades.update().values(plan=plan))
    (view,) = book.recent()
    assert view["status"] == "OPEN"
    assert (view["mark"], view["unrealised"]) == (None, None)
