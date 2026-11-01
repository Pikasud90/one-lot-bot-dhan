"""US-023 AC1: a closed paper trade books its charges per leg, with the REAL sides.

    costs_paise = leg(entry, qty, entry side) + leg(exit, qty, exit side)
    leg         = round_half_up(CostModel.leg(price_rupees, qty, side).total x 100), ONCE per leg
    net_paise   = gross_paise - costs_paise;  risk_paise excludes costs

Sides: a long (stock) is BUY then SELL; a stock SHORT is SELL then BUY, so it pays STT on its
ENTRY leg and stamp duty on its exit; an index option is long premium (BUY then SELL) whichever
way the underlying points. An exit with no price keeps costs and net NULL (never a fake 0).

Worked stock (conftest): RELIANCE long, fill 1000.30 (100030), target 1010.00 (101000), stop
995.00 slipped to 994.70 (99470); equity_intraday rates from config/costs.yaml.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select, text

from onelot.config import CostConfig
from onelot.costs import CostModel
from onelot.paper.book import PaperTrade
from onelot.store.schema import paper_events, paper_trades

from .conftest import SPECS, THU, ist, make_alert, table_rows

Row = tuple[float, float, float, float]
FILL = (1000.00, 1001.00, 999.00, 1000.50)  # the 10:06 fill bar: touches nothing
TARGET_BAR = (1005.00, 1010.05, 1004.00, 1009.00)  # trades through 1010.00
STOP_BAR = (999.00, 999.50, 994.00, 996.00)  # touches 995.00
SHORT_STOP_BAR = (1002.00, 1005.00, 1001.00, 1003.00)  # a short's stop 1005.00
OPT_FILL = (45210.35, 45220.00, 45200.00, 45215.00)
CE_STOP_BAR = (45150.00, 45160.00, 45100.00, 45120.00)  # under the CE stop 45110.35
PE_STOP_BAR = (45250.00, 45320.00, 45240.00, 45300.00)  # over the PE stop 45310.35


def bars(rows: Sequence[Row], start: dt.datetime | None = None) -> pd.DataFrame:
    start = start or ist(THU, 10, 6)
    idx = pd.date_range(start, periods=len(rows), freq="1min")
    o, h, l, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e3}, index=idx)


def feed(**frames: pd.DataFrame | None) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


def at(minute: int, hour: int = 10) -> dt.datetime:
    return ist(THU, hour, minute)


def the_row(engine) -> dict[str, Any]:
    (row,) = table_rows(engine, paper_trades)
    return row


def linear_premium(option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float:
    """312.34 at the alert's 45210.35, delta 0.65: a CE gains as the underlying rises."""
    sign = 1.0 if option["opt_type"] == "CE" else -1.0
    return 312.34 + sign * 0.65 * (underlying - 45210.35)


def leg_oracle(model: CostModel, price_paise: int, qty: int, side: str) -> int:
    """The contract's leg: CostModel.leg(...).total read through its repr, x100, HALF_UP."""
    total = model.leg(price_paise / 100, qty, side).total  # type: ignore[arg-type]
    return int((Decimal(repr(total)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def model_for(cost_cfg: CostConfig, key: str) -> CostModel:
    return CostModel(cost_cfg.product(SPECS[key].product))


class StubCosts:
    """`cost_for` answering a fixed total per side; records every call (rupees, qty, side)."""

    def __init__(self, totals: Mapping[str, float] | None = None) -> None:
        self.totals = dict(totals or {"buy": 0.125, "sell": 0.625})
        self.asked: list[str] = []
        self.legs: list[tuple[float, int, str]] = []

    def cost_for(self, instrument_key: str) -> Any:
        self.asked.append(instrument_key)
        outer = self

        class Model:
            def leg(self, premium: float, qty: int, side: str) -> Any:
                outer.legs.append((premium, qty, side))
                return SimpleNamespace(total=outer.totals[side])

            def round_trip_cost(self, *_: Any) -> float:
                raise AssertionError("each leg is costed on its own side, never round_trip_cost")

        return Model()


def close_stock(make_book, alert=None, rows=(FILL, TARGET_BAR), **kw: Any):
    book = make_book(**kw)
    book.on_alert(alert or make_alert(), ist(THU, 10, 5, 4))
    events = book.step(at(8), feed(RELIANCE=bars(list(rows))))
    return book, events


def close_option(make_book, direction: str = "long", rows=None, **kw: Any):
    book = make_book(premium_at=kw.pop("premium_at", linear_premium), **kw)
    book.on_alert(make_alert("INDEX", direction), ist(THU, 10, 5, 4))
    stop_bar = CE_STOP_BAR if direction == "long" else PE_STOP_BAR
    events = book.step(at(8), feed(BANKNIFTY=bars(rows or [OPT_FILL, stop_bar])))
    return book, events


# ── the pure leg arithmetic ───────────────────────────────────────────────────────────
class TestSides:
    @pytest.mark.parametrize(
        "kind, direction, expected",
        [
            ("STOCK", "long", ("buy", "sell")),
            ("STOCK", "short", ("sell", "buy")),
            ("INDEX", "long", ("buy", "sell")),
            ("INDEX", "short", ("buy", "sell")),  # a BUY_PE is long premium
        ],
    )
    def test_the_real_sides(self, kind, direction, expected):
        from onelot.paper.charges import leg_sides

        assert leg_sides(kind, direction) == expected

    @pytest.mark.parametrize("kind, direction", [("STOCK", "flat"), ("FUTURE", "long"), ("", "")])
    def test_an_unknown_kind_or_direction_is_refused_never_guessed(self, kind, direction):
        from onelot.paper.charges import leg_sides

        with pytest.raises(ValueError, match="paper"):
            leg_sides(kind, direction)


class TestLegPaise:
    @pytest.mark.parametrize(
        "total, paise",
        [
            (0.125, 13),  # 12.5 -> 13: half UP (half-even and truncation give 12)
            (0.625, 63),  # 62.5 -> 63
            (0.4215324218, 42),
            (0.0, 0),
            (1.005, 101),  # read through its repr "1.005", like money.to_decimal: 100.5 -> 101
            (123456.785, 12345679),
        ],
    )
    def test_one_leg_is_rounded_once_half_up_from_the_totals_repr(self, total, paise):
        from onelot.paper.charges import leg_paise

        stub = StubCosts({"buy": total, "sell": total})
        assert leg_paise(stub.cost_for("X"), 100030, 1, "buy") == paise

    def test_the_model_is_asked_in_rupees_as_a_float_with_the_qty_and_side(self):
        from onelot.paper.charges import leg_paise

        stub = StubCosts()
        leg_paise(stub.cost_for("X"), 45280, 30, "sell")
        assert stub.legs == [(452.8, 30, "sell")]
        assert isinstance(stub.legs[0][0], float)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, None, True, "0.42"])
    def test_a_total_that_is_not_a_finite_non_negative_number_raises(self, bad):
        from onelot.paper.charges import leg_paise

        stub = StubCosts({"buy": bad, "sell": bad})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="paper"):
            leg_paise(stub.cost_for("X"), 100030, 1, "buy")

    @pytest.mark.parametrize("qty", [0, -1])
    def test_a_qty_below_one_raises(self, qty):
        from onelot.paper.charges import leg_paise

        with pytest.raises(ValueError, match="qty"):
            leg_paise(StubCosts().cost_for("X"), 100030, qty, "buy")


class TestRoundTripPaise:
    def test_each_leg_is_rounded_on_its_own_never_the_sum(self):
        from onelot.paper.charges import round_trip_paise

        stub = StubCosts({"buy": 0.125, "sell": 0.125})  # 12.5 + 12.5: 26, the sum rounds to 25
        model = stub.cost_for("X")
        assert round_trip_paise(model, "STOCK", "long", 100030, 101000, 1) == 26

    @pytest.mark.parametrize(
        "kind, direction, first, second",
        [("STOCK", "long", "buy", "sell"), ("STOCK", "short", "sell", "buy")]
        + [("INDEX", "long", "buy", "sell"), ("INDEX", "short", "buy", "sell")],
    )
    def test_the_entry_leg_then_the_exit_leg_on_their_real_sides(
        self, kind, direction, first, second
    ):
        from onelot.paper.charges import round_trip_paise

        stub = StubCosts()
        round_trip_paise(stub.cost_for("X"), kind, direction, 100030, 101000, 1)
        assert stub.legs == [(1000.3, 1, first), (1010.0, 1, second)]

    def test_a_short_pays_stt_on_its_entry_leg(self, cost_cfg):
        """At Rs 1 lakh a share (MRF-like), STT 0.025 % (Rs 25) on the sell leg and stamp 0.003 %
        (Rs 3) on the buy leg: the sides are rupees apart, far beyond any rounding."""
        from onelot.paper.charges import round_trip_paise

        model = model_for(cost_cfg, "RELIANCE")  # equity_intraday
        entry, exit_ = 9_997_000, 9_800_000  # sold at 99,970.00, bought back at 98,000.00
        short = round_trip_paise(model, "STOCK", "short", entry, exit_, 1)
        assert short == leg_oracle(model, entry, 1, "sell") + leg_oracle(model, exit_, 1, "buy")
        assert short == 8254  # the Tester's MRF golden value (US-023 AC1)
        assumed_long = leg_oracle(model, entry, 1, "buy") + leg_oracle(model, exit_, 1, "sell")
        assert assumed_long == 8211 and short - assumed_long == 43


# ── the book books them with the close ────────────────────────────────────────────────
class TestTheBookBooksCostsAtTheClose:
    def test_the_worked_long_target(self, make_book, engine):
        close_stock(make_book)
        row = the_row(engine)
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (100030, 101000, 970)
        # BUY 1000.30: 42.153 paise -> 42; SELL 1010.00: 64.782 paise -> 65 (hand-derived)
        assert row["costs_paise"] == 107
        assert row["net_paise"] == 970 - 107 == 863

    def test_a_long_stop(self, make_book, engine, cost_cfg):
        close_stock(make_book, rows=(FILL, STOP_BAR))
        row = the_row(engine)
        model = model_for(cost_cfg, "RELIANCE")
        assert row["close_reason"] == "STOP" and row["exit_paise"] == 99470
        expected = leg_oracle(model, 100030, 1, "buy") + leg_oracle(model, 99470, 1, "sell")
        assert row["costs_paise"] == expected
        assert row["net_paise"] == row["gross_paise"] - expected == -560 - expected

    def test_a_short_is_costed_sell_then_buy(self, make_book, engine, cost_cfg):
        close_stock(make_book, make_alert(direction="short"), (FILL, SHORT_STOP_BAR))
        row = the_row(engine)
        assert (row["entry_paise"], row["exit_paise"]) == (99970, 100535)
        model = model_for(cost_cfg, "RELIANCE")
        expected = leg_oracle(model, 99970, 1, "sell") + leg_oracle(model, 100535, 1, "buy")
        assert row["costs_paise"] == expected
        assert row["net_paise"] == -(100535 - 99970) - expected

    def test_a_short_asks_the_model_for_a_sell_leg_then_a_buy_leg(self, make_book, engine):
        stub = StubCosts()
        alert = make_alert(direction="short")
        close_stock(make_book, alert, (FILL, SHORT_STOP_BAR), cost_for=stub.cost_for)
        assert stub.legs == [(999.7, 1, "sell"), (1005.35, 1, "buy")]
        assert stub.asked == ["RELIANCE"]
        assert the_row(engine)["costs_paise"] == 63 + 13

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_an_option_is_costed_buy_then_sell_of_one_lot_on_its_premiums(
        self, make_book, engine, cost_cfg, direction
    ):
        close_option(make_book, direction)
        row = the_row(engine)
        assert row["close_reason"] == "STOP" and row["qty"] == 30
        model = model_for(cost_cfg, "BANKNIFTY_OPT")
        entry, exit_ = row["entry_paise"], row["exit_paise"]
        expected = leg_oracle(model, entry, 30, "buy") + leg_oracle(model, exit_, 30, "sell")
        assert row["costs_paise"] == expected
        assert row["net_paise"] == (exit_ - entry) * 30 - expected

    def test_an_option_is_asked_for_by_its_instrument_key(self, make_book, engine):
        stub = StubCosts()
        close_option(make_book, "short", cost_for=stub.cost_for)
        row = the_row(engine)
        entry, exit_ = row["entry_paise"] / 100, row["exit_paise"] / 100
        assert stub.legs == [(entry, 30, "buy"), (exit_, 30, "sell")]
        assert stub.asked == ["BANKNIFTY_OPT"]
        assert row["costs_paise"] == 13 + 63

    def test_risk_excludes_costs_and_does_not_move_at_the_close(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        frame = bars([FILL, TARGET_BAR])
        book.step(at(7), feed(RELIANCE=frame))  # only the fill bar has closed
        before = the_row(engine)
        assert before["status"] == "OPEN" and before["costs_paise"] is None
        book.step(at(8), feed(RELIANCE=frame))
        row = the_row(engine)
        assert row["status"] == "CLOSED" and row["costs_paise"] > 0
        assert row["risk_paise"] == before["risk_paise"] == (100030 - 99500)

    def test_costs_and_net_are_stored_as_sql_integers(self, make_book, engine):
        close_stock(make_book)
        with engine.connect() as conn:
            kinds = conn.execute(
                text("SELECT typeof(costs_paise), typeof(net_paise) FROM paper_trades")
            )
            assert kinds.one() == ("integer", "integer")

    @pytest.mark.parametrize("rows", [(FILL, TARGET_BAR), (FILL, STOP_BAR)], ids=["target", "stop"])
    def test_a_longs_total_is_within_one_paisa_of_round_trip_cost(
        self, make_book, engine, cost_cfg, rows
    ):
        close_stock(make_book, rows=rows)
        row = the_row(engine)
        model = model_for(cost_cfg, "RELIANCE")
        exact = model.round_trip_cost(row["entry_paise"] / 100, row["exit_paise"] / 100, 1)
        assert abs(Decimal(row["costs_paise"]) - Decimal(repr(exact)) * 100) <= 1


class TestNoPriceNoCosts:
    def test_an_unpriced_option_exit_keeps_costs_and_net_null_and_costs_nothing(
        self, make_book, engine
    ):
        def premium_at(option: Mapping[str, Any], underlying: float, ts: dt.datetime):
            return linear_premium(option, underlying, ts) if ts <= at(6) else None

        stub = StubCosts()
        close_option(make_book, premium_at=premium_at, cost_for=stub.cost_for)
        row = the_row(engine)
        assert (row["status"], row["exit_paise"], row["gross_paise"]) == ("CLOSED", None, None)
        assert row["costs_paise"] is None and row["net_paise"] is None
        assert stub.legs == []

    def test_a_no_fill_keeps_zero_money(self, make_book, engine):
        stub = StubCosts()
        book = make_book(cost_for=stub.cost_for)
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        gap = (990.00, 991.00, 989.00, 990.50)  # opens through the 995.00 stop
        book.step(at(8), feed(RELIANCE=bars([gap, gap])))
        row = the_row(engine)
        assert row["status"] == "NO_FILL"
        assert (row["gross_paise"], row["costs_paise"], row["net_paise"]) == (0, 0, 0)
        assert stub.legs == []

    @pytest.mark.parametrize("failure", ["raises", "nan"])
    def test_a_cost_model_that_fails_still_closes_the_trade_with_no_costs_and_says_so(
        self, make_book, engine, caplog, failure
    ):
        def cost_for(key: str) -> Any:
            if failure == "raises":
                raise KeyError(f"no cost table for {key}")
            return StubCosts({"buy": 0.42, "sell": float("nan")}).cost_for(key)

        book, events = close_stock(make_book, cost_for=cost_for)
        row = the_row(engine)
        assert (row["status"], row["close_reason"], row["exit_paise"]) == (
            "CLOSED",
            "TARGET",
            101000,
        )
        assert row["gross_paise"] == 970
        assert row["costs_paise"] is None and row["net_paise"] is None  # never a fake 0
        unpriced = [e for e in events if e.kind == "COSTS_UNAVAILABLE"]
        assert len(unpriced) == 1 and unpriced[0].ts == at(7)
        assert "error" in unpriced[0].detail
        kinds = [e["kind"] for e in table_rows(engine, paper_events)]
        assert kinds == ["OPENED", "FILLED", "CLOSED", "COSTS_UNAVAILABLE"]
        assert book.status()["hook_errors"] == 1 and book.status()["closed"] == 1


# ── carry-over: money read back from a row is never truncated ─────────────────────────
def _stored_row(engine, alert_id: str = "a1b2c3d4e5f6") -> Mapping[str, Any]:
    with engine.connect() as conn:
        return (
            conn.execute(select(paper_trades).where(paper_trades.c.alert_id == alert_id))
            .mappings()
            .one()
        )


class TestFromRowNeverTruncatesMoney:
    @pytest.fixture(autouse=True)
    def _fresh_warnings(self):
        """'Warned once' is per process: start each test with nothing warned yet."""
        from onelot.paper import book as book_module

        book_module._MONEY_WARNED.clear()
        yield
        book_module._MONEY_WARNED.clear()

    def test_a_non_integer_paise_value_is_unknown_and_warned_once(self, make_book, engine, caplog):
        close_stock(make_book, rows=(FILL, FILL))  # filled, still OPEN: risk 530
        with engine.begin() as conn:
            conn.execute(text("UPDATE paper_trades SET risk_paise = 1030.5"))
        row = _stored_row(engine)
        assert row["risk_paise"] == 1030.5  # SQLite kept the REAL
        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            first = PaperTrade.from_row(row)
            second = PaperTrade.from_row(row)
        assert first.risk_paise is None and second.risk_paise is None  # never 1030
        warned = [r for r in caplog.records if "risk_paise" in r.getMessage()]
        assert len(warned) == 1, [r.getMessage() for r in caplog.records]
        assert str(first.id) in warned[0].getMessage()

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), "12x", b"\x00", True])
    def test_a_value_that_is_not_a_number_is_unknown(self, make_book, engine, value):
        close_stock(make_book, rows=(FILL, FILL))
        row = dict(_stored_row(engine))
        row["entry_paise"] = value
        assert PaperTrade.from_row(row).entry_paise is None

    def test_a_whole_float_is_exact_paise(self, make_book, engine):
        close_stock(make_book, rows=(FILL, FILL))
        row = dict(_stored_row(engine))
        row["entry_paise"] = 100030.0
        trade = PaperTrade.from_row(row)
        assert trade.entry_paise == 100030 and type(trade.entry_paise) is int


# ── carry-over: one corrupt active row never stops the book from starting ─────────────
class TestACorruptActiveRowIsSkipped:
    @pytest.mark.parametrize(
        "corruption",
        ["grade = 'bogus'", "plan = 'not json {'", "created_at = 'garbage'"],
    )
    def test_the_book_starts_with_the_others_and_counts_the_skipped_row(
        self, make_book, engine, caplog, corruption
    ):
        book = make_book()
        book.on_alert(make_alert(alert_id="good"), ist(THU, 10, 5, 4))
        book.on_alert(make_alert(alert_id="bad"), ist(THU, 10, 5, 4))
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE paper_trades SET {corruption} WHERE alert_id = 'bad'"))
        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            restarted = make_book()
        assert [t.alert_id for t in restarted.open_trades()] == ["good"]
        assert restarted.status()["hook_errors"] == 1
        assert any("skipped" in r.getMessage() for r in caplog.records)

    def test_a_locked_database_at_startup_still_fails_loudly(self, make_book, engine, monkeypatch):
        """Skipping is for one bad ROW; a database that cannot be read is not 'corrupt rows'
        (silently starting with no trades would leave them unmanaged)."""
        from sqlalchemy.exc import OperationalError

        book = make_book()
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        from onelot.paper import book as book_module

        def locked(*_: Any, **__: Any) -> Any:
            raise OperationalError("SELECT", {}, Exception("database is locked"))

        monkeypatch.setattr(book_module.PaperTrade, "from_row", classmethod(locked))
        with pytest.raises(OperationalError):
            make_book()
