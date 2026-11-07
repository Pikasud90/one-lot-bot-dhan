"""The paper book prices an index-option trade from the contract's TRADED prices when it can.

    PaperBook(..., real_premium_at=None)
    entry fill, exit fill and the open trade's mark each ask `real_premium_at(option, underlying,
    ts)` first. A real premium is taken as it is (no live-chain basis) and recorded:
    the FILLED event says `premium_source` "real"|"model", the CLOSED event `exit_premium_source`.
    A source that raises, or answers None, NaN, infinity, zero or less, leaves that leg to the
    Black-76 model plus the basis, exactly as before.

Worked option (conftest): BUY_CE BANKNIFTY, alert entry 45,210.35, premium 312.34. The model used
here moves 0.65 per index point, like the marks tests'. Expected fills come from Decimal oracles,
never from the code under test.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pandas as pd
import pytest

from onelot.config import CostConfig, PaperCfg
from onelot.costs import CostModel
from onelot.paper.book import PaperBook
from onelot.paper.money import to_paise
from onelot.store.schema import paper_events, paper_trades

from .conftest import BT, SPECS, THU, ist, make_alert, make_bars, table_rows

Row = tuple[float, float, float, float]
OPT_FILL: Row = (45210.35, 45220.00, 45200.00, 45215.00)  # the 10:06 bar: fills at its open
QUIET: Row = (
    45215.00,
    45230.00,
    45190.00,
    45225.00,
)  # inside the 45,110.35 stop / 45,410.35 target
THROUGH: Row = (45215.00, 45420.00, 45200.00, 45400.00)  # trades through the 45,410.35 target
DOWN: Row = (45215.00, 45220.00, 45100.00, 45105.00)  # trades through the 45,110.35 stop


def bars(rows: Sequence[Row]) -> pd.DataFrame:
    idx = pd.date_range(ist(THU, 10, 6), periods=len(rows), freq="1min")
    o, h, lo, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1e3}, index=idx)


def feed(**frames: pd.DataFrame) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


def ask_paise(premium: str, spread: str = "0.006", ticks: int = 2) -> int:
    """Oracle: ceil_tick(premium x (1 + spread) + ticks x 0.05), in paise."""
    raw = Decimal(premium) * (1 + Decimal(spread)) + ticks * Decimal("0.05")
    return int((raw / Decimal("0.05")).to_integral_value(rounding=ROUND_CEILING)) * 5


def bid_paise(premium: str, spread: str = "0.006", ticks: int = 2) -> int:
    """Oracle: floor_tick(premium x (1 - spread) - ticks x 0.05), at least one tick, in paise."""
    raw = Decimal(premium) * (1 - Decimal(spread)) - ticks * Decimal("0.05")
    return max(int((raw / Decimal("0.05")).to_integral_value(rounding=ROUND_FLOOR)) * 5, 5)


class Model:
    """The Black-76 stand-in: 312.34 at 45,210.35, 0.65 per point, never under one tick."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dt.datetime]] = []

    @staticmethod
    def fair(underlying: float) -> float:
        return max(0.05, 312.34 + 0.65 * (underlying - 45210.35))

    def __call__(self, option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float:
        self.calls.append((float(underlying), ts))
        return self.fair(underlying)


class Real:
    """A `real_premium_at` that records what it was asked and answers from `fn`."""

    def __init__(self, fn: Callable[[Mapping[str, Any], float, dt.datetime], Any] | None = None):
        self.fn = fn or (lambda option, underlying, ts: None)
        self.calls: list[tuple[dict[str, Any], float, dt.datetime]] = []

    def __call__(self, option: Mapping[str, Any], underlying: float, ts: dt.datetime):
        self.calls.append((dict(option), float(underlying), ts))
        return self.fn(option, underlying, ts)


@pytest.fixture
def model() -> Model:
    return Model()


@pytest.fixture
def make_real_book(engine, cost_cfg: CostConfig, model: Model) -> Callable[..., PaperBook]:
    models = {k: CostModel(cost_cfg.product(s.product)) for k, s in SPECS.items()}

    def _make(
        real: Callable[..., Any] | None = None, premium_at: Callable[..., Any] | None = None
    ) -> PaperBook:
        return PaperBook(
            engine,
            PaperCfg(enabled=True, feed_delay_minutes=15, reconcile_days=3),
            BT,
            SPECS,
            models.__getitem__,
            premium_at or model,
            clock=lambda: ist(THU, 10, 8),
            real_premium_at=real,
        )

    return _make


def chain_alert(chain: float = 300.00) -> dict[str, Any]:
    """The worked option quoted on the live chain at `chain` (the model says 312.34): a basis."""
    alert = make_alert("INDEX")
    alert["economics"]["entry_price"] = chain
    alert["economics"]["synthetic"] = False
    return alert


def the_row(engine) -> dict[str, Any]:
    (row,) = table_rows(engine, paper_trades)
    return row


def events(engine, kind: str) -> list[dict[str, Any]]:
    return [e for e in table_rows(engine, paper_events) if e["kind"] == kind]


def run(book: PaperBook, alert: dict[str, Any], rows: Sequence[Row], at=None) -> None:
    book.on_alert(alert, ist(THU, 10, 5, 4))
    book.step(at or ist(THU, 10, 8), feed(BANKNIFTY=bars(rows)))


# ── the entry ─────────────────────────────────────────────────────────────────────────────
class TestEntry:
    def test_a_real_premium_is_the_fill_price_with_the_models_friction(
        self, make_real_book, engine
    ):
        real = Real(lambda option, u, ts: 300.00 if u == 45210.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        row = the_row(engine)
        assert row["status"] == "OPEN"
        assert row["entry_paise"] == ask_paise("300.00") == 30190  # not the model's 31435
        (filled,) = events(engine, "FILLED")
        assert filled["detail"]["premium_source"] == "real"
        assert filled["detail"]["fair_premium_paise"] == 30000
        assert filled["detail"]["entry_paise"] == 30190

    def test_it_is_asked_for_the_fill_bars_open_at_the_fill_bars_time(self, make_real_book):
        real = Real(lambda option, u, ts: 300.00)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        option, underlying, ts = real.calls[0]
        assert option == make_alert("INDEX")["economics"]["option"]  # frozen at the alert
        assert (underlying, ts) == (45210.35, ist(THU, 10, 6))

    def test_the_stop_leg_of_the_risk_is_asked_at_the_stop_level(self, make_real_book, engine):
        def answer(option, u, ts):
            return {45210.35: 300.00, 45110.35: 240.00}.get(u)

        real = Real(answer)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        assert [round(c[1], 2) for c in real.calls[:2]] == [45210.35, 45110.35]
        assert the_row(engine)["risk_paise"] == (ask_paise("300.00") - bid_paise("240.00")) * 30

    def test_a_stop_leg_the_source_cannot_price_is_priced_by_the_model(
        self, make_real_book, engine, model
    ):
        real = Real(lambda option, u, ts: 300.00 if u == 45210.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        stop_fair = model.fair(45110.35)  # 247.34
        assert (
            the_row(engine)["risk_paise"]
            == (ask_paise("300.00") - bid_paise(f"{stop_fair:.2f}")) * 30
        )

    def test_a_live_chain_alert_takes_the_real_premium_without_the_basis(
        self, make_real_book, engine
    ):
        real = Real(lambda option, u, ts: 305.00 if u == 45210.35 else None)
        run(make_real_book(real), chain_alert(300.00), [OPT_FILL])
        row = the_row(engine)
        assert row["plan"]["pricing"]["basis_paise"] == -1234  # recorded at the alert
        assert row["entry_paise"] == ask_paise("305.00")  # but not applied to a real price
        (filled,) = events(engine, "FILLED")
        assert filled["detail"]["premium_source"] == "real"
        assert "basis_paise" not in filled["detail"]
        assert filled["detail"]["fair_premium_paise"] == 30500

    def test_a_real_premium_below_one_tick_is_floored_at_one_tick(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 0.01 if u == 45210.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        (filled,) = events(engine, "FILLED")
        assert filled["detail"]["fair_premium_paise"] == 5  # one tick: 0.05
        assert the_row(engine)["entry_paise"] == ask_paise("0.05")

    def test_the_source_gets_a_copy_so_it_cannot_change_the_frozen_option(
        self, make_real_book, engine
    ):
        def vandal(option, u, ts):
            option["strike"] = 1.0  # a hostile source
            option["opt_type"] = "PE"
            return 300.00 if u == 45210.35 else None

        run(make_real_book(Real(vandal)), make_alert("INDEX"), [OPT_FILL])
        frozen = the_row(engine)["plan"]["alert"]["economics"]["option"]
        assert frozen["strike"] == 45000.0 and frozen["opt_type"] == "CE"

    def test_a_stock_is_never_priced_from_an_option_source(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 300.00)
        book = make_real_book(real)
        book.on_alert(make_alert("STOCK"), ist(THU, 10, 5, 4))
        book.step(ist(THU, 10, 8), feed(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0, 1000.0])))
        assert the_row(engine)["status"] == "OPEN" and real.calls == []
        (filled,) = events(engine, "FILLED")
        assert "premium_source" not in filled["detail"]

    def test_an_alert_with_no_option_dict_never_asks_the_source(self, make_real_book, engine):
        alert = make_alert("INDEX")
        alert["economics"]["option"] = {}
        real = Real(lambda option, u, ts: 300.00)
        run(make_real_book(real), alert, [OPT_FILL])
        assert real.calls == []


# ── the fall back to the model ─────────────────────────────────────────────────────────────
class TestFallsBackToTheModel:
    def test_a_source_that_answers_none_leaves_the_fill_to_the_model(
        self, make_real_book, engine, model
    ):
        real = Real()
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        row = the_row(engine)
        assert row["entry_paise"] == ask_paise("312.34") == 31435
        (filled,) = events(engine, "FILLED")
        assert filled["detail"]["premium_source"] == "model"
        assert "basis_paise" not in filled["detail"]  # a synthetic alert has none
        assert real.calls, "the source is asked first"
        assert model.calls, "then the model"

    def test_a_live_chain_alert_falls_back_to_model_plus_basis_and_says_so(
        self, make_real_book, engine
    ):
        run(make_real_book(Real()), chain_alert(300.00), [OPT_FILL])
        row = the_row(engine)
        assert row["entry_paise"] == ask_paise("300.00") == 30190  # 312.34 - 12.34 = 300.00
        (filled,) = events(engine, "FILLED")
        assert filled["detail"]["premium_source"] == "model"
        assert filled["detail"]["basis_paise"] == -1234

    def test_without_any_source_the_book_is_what_it_was(self, make_real_book, engine):
        book = make_real_book(None)
        assert book.real_premium_at is None
        run(book, make_alert("INDEX"), [OPT_FILL])
        assert the_row(engine)["entry_paise"] == ask_paise("312.34")
        (filled,) = events(engine, "FILLED")
        assert filled["detail"]["premium_source"] == "model"

    @pytest.mark.parametrize(
        "answer",
        [None, float("nan"), float("inf"), float("-inf"), 0, 0.0, -1.0, -312.34],
        ids=["none", "nan", "inf", "-inf", "int-zero", "zero", "negative", "negative-premium"],
    )
    def test_an_answer_that_is_not_a_positive_finite_price_is_not_used(
        self, make_real_book, engine, answer
    ):
        run(make_real_book(Real(lambda option, u, ts: answer)), make_alert("INDEX"), [OPT_FILL])
        assert the_row(engine)["entry_paise"] == ask_paise("312.34")
        assert events(engine, "FILLED")[0]["detail"]["premium_source"] == "model"

    def test_a_source_that_raises_is_logged_quietly_and_the_model_prices_it(
        self, make_real_book, engine, caplog
    ):
        def boom(option, u, ts):
            raise RuntimeError("DH-904 rate limit")

        with caplog.at_level(logging.DEBUG, logger="onelot.paper.book"):
            run(make_real_book(Real(boom)), make_alert("INDEX"), [OPT_FILL])
        assert the_row(engine)["entry_paise"] == ask_paise("312.34")
        assert events(engine, "FILLED")[0]["detail"]["premium_source"] == "model"
        assert any("real premium unavailable" in r.message for r in caplog.records)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_raising_source_never_counts_as_a_book_failure(self, make_real_book, engine):
        def boom(option, u, ts):
            raise ValueError("no bars")

        book = make_real_book(Real(boom))
        run(book, make_alert("INDEX"), [OPT_FILL])
        assert book.status()["hook_errors"] == 0 and book.status()["filled"] == 1

    def test_a_trade_neither_source_can_price_is_unpriced_never_invented(
        self, make_real_book, engine
    ):
        run(
            make_real_book(Real(), premium_at=lambda o, u, ts: None),
            make_alert("INDEX"),
            [OPT_FILL],
        )
        row = the_row(engine)
        assert row["status"] == "PENDING_FILL" and row["entry_paise"] is None
        (event,) = events(engine, "PRICING_UNAVAILABLE")
        assert event["detail"]["leg"] == "entry"

    def test_the_source_can_fail_one_leg_and_answer_the_next(self, make_real_book, engine):
        answers = iter([300.00, RuntimeError("blip"), 295.00])

        def flaky(option, u, ts):
            a = next(answers, None)
            if isinstance(a, Exception):
                raise a
            return a

        run(make_real_book(Real(flaky)), make_alert("INDEX"), [OPT_FILL])
        assert the_row(engine)["entry_paise"] == ask_paise("300.00")  # the first answer


# ── the exit ──────────────────────────────────────────────────────────────────────────────
class TestExit:
    def test_a_target_exit_is_sold_at_the_real_price_with_the_models_friction(
        self, make_real_book, engine
    ):
        def answer(option, u, ts):
            return {45210.35: 300.00, 45110.35: 240.00, 45410.35: 450.00}.get(round(u, 2))

        book = make_real_book(Real(answer))
        run(book, make_alert("INDEX"), [OPT_FILL, THROUGH])
        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET")
        assert row["exit_paise"] == bid_paise("450.00") == 44720
        assert row["gross_paise"] == (44720 - ask_paise("300.00")) * 30
        (closed,) = events(engine, "CLOSED")
        assert closed["detail"]["exit_premium_source"] == "real"
        assert closed["detail"]["exit_paise"] == 44720

    def test_a_stop_exit_asks_the_source_at_the_stop_level(self, make_real_book, engine):
        real = Real(lambda option, u, ts: {45210.35: 300.00, 45110.35: 240.00}.get(round(u, 2)))
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, DOWN])
        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "STOP")
        assert row["exit_paise"] == bid_paise("240.00")
        assert events(engine, "CLOSED")[0]["detail"]["exit_premium_source"] == "real"

    def test_the_exit_source_is_recorded_per_leg_a_real_entry_can_have_a_model_exit(
        self, make_real_book, engine, model
    ):
        real = Real(lambda option, u, ts: 300.00 if round(u, 2) == 45210.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, THROUGH])
        assert events(engine, "FILLED")[0]["detail"]["premium_source"] == "real"
        closed = events(engine, "CLOSED")[0]["detail"]
        assert closed["exit_premium_source"] == "model"
        assert closed["exit_paise"] == bid_paise(f"{model.fair(45410.35):.2f}")

    def test_a_model_entry_can_have_a_real_exit(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 450.00 if round(u, 2) == 45410.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, THROUGH])
        assert events(engine, "FILLED")[0]["detail"]["premium_source"] == "model"
        assert events(engine, "CLOSED")[0]["detail"]["exit_premium_source"] == "real"

    def test_a_live_chain_exit_takes_the_real_price_without_the_basis(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 450.00 if round(u, 2) == 45410.35 else None)
        run(make_real_book(real), chain_alert(300.00), [OPT_FILL, THROUGH])
        assert the_row(engine)["exit_paise"] == bid_paise("450.00")  # not 450.00 - 12.34

    def test_without_a_source_the_exit_is_the_models_plus_basis_as_before(
        self, make_real_book, engine
    ):
        run(make_real_book(None), chain_alert(300.00), [OPT_FILL, THROUGH])
        assert the_row(engine)["exit_paise"] == bid_paise("430.00")  # 442.34 - 12.34
        assert events(engine, "CLOSED")[0]["detail"]["exit_premium_source"] == "model"

    def test_an_exit_no_source_can_price_has_no_source_to_record(self, make_real_book, engine):
        book = make_real_book(Real(), premium_at=lambda o, u, ts: None if u > 45300 else 312.34)
        run(book, make_alert("INDEX"), [OPT_FILL, THROUGH])
        row = the_row(engine)
        assert row["status"] == "CLOSED" and row["exit_paise"] is None
        assert "exit_premium_source" not in events(engine, "CLOSED")[0]["detail"]

    def test_a_stock_exit_records_no_premium_source(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 300.00)
        book = make_real_book(real)
        book.on_alert(make_alert("STOCK"), ist(THU, 10, 5, 4))
        rows = make_bars(ist(THU, 10, 5), [1000.0, 1000.0, 1011.0, 1011.0])
        rows.loc[rows.index[2], "high"] = 1012.0  # reaches the 1,010 target
        book.step(ist(THU, 10, 10), feed(RELIANCE=rows))
        assert the_row(engine)["status"] == "CLOSED" and real.calls == []
        assert "exit_premium_source" not in events(engine, "CLOSED")[0]["detail"]

    def test_a_raising_source_at_the_exit_falls_back_to_the_model(self, make_real_book, engine):
        def answer(option, u, ts):
            if round(u, 2) == 45410.35:
                raise RuntimeError("DH-904 rate limit")
            return 300.00 if round(u, 2) == 45210.35 else None

        run(make_real_book(Real(answer)), make_alert("INDEX"), [OPT_FILL, THROUGH])
        row = the_row(engine)
        assert row["status"] == "CLOSED"
        assert events(engine, "CLOSED")[0]["detail"]["exit_premium_source"] == "model"


# ── the mark ──────────────────────────────────────────────────────────────────────────────
def position(engine) -> dict[str, Any]:
    row = the_row(engine)
    assert row["status"] == "OPEN", row["status"]
    return dict(row["plan"]["position"])


class TestMark:
    def test_an_open_trade_is_marked_at_the_real_price_of_its_last_close(
        self, make_real_book, engine
    ):
        real = Real(lambda option, u, ts: {45210.35: 300.00, 45225.0: 306.40}.get(round(u, 2)))
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, QUIET])
        assert position(engine)["mark_paise"] == to_paise(306.40) == 30640

    def test_the_mark_is_asked_at_the_last_close_and_its_close_time(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 306.40 if round(u, 2) == 45225.0 else 300.00)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, QUIET])
        marks = [c for c in real.calls if c[1] == 45225.0]
        assert marks and marks[-1][2] == ist(THU, 10, 8)  # the 10:07 bar's close time

    def test_a_live_chain_mark_takes_the_real_price_without_the_basis(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 306.40 if round(u, 2) == 45225.0 else None)
        run(make_real_book(real), chain_alert(300.00), [OPT_FILL, QUIET])
        assert position(engine)["mark_paise"] == 30640  # not 30640 - 1234

    def test_a_mark_the_source_cannot_price_is_the_model_plus_basis(
        self, make_real_book, engine, model
    ):
        run(make_real_book(Real()), chain_alert(300.00), [OPT_FILL, QUIET])
        assert position(engine)["mark_paise"] == to_paise(model.fair(45225.0)) - 1234

    def test_a_real_mark_below_one_tick_is_one_tick_not_zero(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 0.01 if round(u, 2) == 45225.0 else 300.00)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, QUIET])
        assert position(engine)["mark_paise"] == 5

    @pytest.mark.parametrize("answer", [float("nan"), float("inf"), 0.0, -4.0])
    def test_an_unusable_real_mark_falls_back_to_the_model(
        self, make_real_book, engine, model, answer
    ):
        real = Real(lambda option, u, ts: answer if round(u, 2) == 45225.0 else 300.00)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL, QUIET])
        assert position(engine)["mark_paise"] == to_paise(model.fair(45225.0))

    def test_a_raising_source_at_the_mark_never_stops_the_step(self, make_real_book, engine, model):
        def answer(option, u, ts):
            if round(u, 2) == 45225.0:
                raise RuntimeError("DH-904")
            return 300.00

        run(make_real_book(Real(answer)), make_alert("INDEX"), [OPT_FILL, QUIET])
        assert position(engine)["mark_paise"] == to_paise(model.fair(45225.0))
        assert "PRICING_UNAVAILABLE" not in [e["kind"] for e in table_rows(engine, paper_events)]

    def test_the_mark_follows_the_source_as_bars_arrive(self, make_real_book, engine):
        real = Real(
            lambda option, u, ts: {45225.0: 306.40, 45190.0: 281.10}.get(round(u, 2), 300.0)
        )
        book = make_real_book(real)
        book.on_alert(make_alert("INDEX"), ist(THU, 10, 5, 4))
        book.step(ist(THU, 10, 8), feed(BANKNIFTY=bars([OPT_FILL, QUIET])))
        first = position(engine)["mark_paise"]
        down = (45225.0, 45230.0, 45185.0, 45190.0)
        book.step(ist(THU, 10, 9), feed(BANKNIFTY=bars([OPT_FILL, QUIET, down])))
        assert (first, position(engine)["mark_paise"]) == (30640, 28110)

    def test_a_stock_has_no_mark_and_never_asks(self, make_real_book, engine):
        real = Real(lambda option, u, ts: 300.00)
        book = make_real_book(real)
        book.on_alert(make_alert("STOCK"), ist(THU, 10, 5, 4))
        book.step(ist(THU, 10, 8), feed(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 3)))
        assert real.calls == []
        assert "mark_paise" not in the_row(engine)["plan"]["position"]


class TestSourceValueTypes:
    @pytest.mark.parametrize("answer", [300, 300.0])
    def test_an_int_or_a_float_is_a_price(self, make_real_book, engine, answer):
        real = Real(lambda option, u, ts: answer if round(u, 2) == 45210.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        assert the_row(engine)["entry_paise"] == ask_paise("300.00")

    def test_a_numpy_float_is_a_price(self, make_real_book, engine):
        import numpy as np

        real = Real(lambda option, u, ts: np.float64(300.00) if round(u, 2) == 45210.35 else None)
        run(make_real_book(real), make_alert("INDEX"), [OPT_FILL])
        assert the_row(engine)["entry_paise"] == ask_paise("300.00")
