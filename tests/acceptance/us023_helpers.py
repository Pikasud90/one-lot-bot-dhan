"""Offline harness shared by the US-023 acceptance tests (paper ledger in integer paise).

Nothing here touches the network, a broker or the wall clock. It extends `us001_helpers` and
`us002_helpers` (imported, never copied or edited). The new module `onelot.paper.ledger` is
imported lazily, inside `ledger_module()`, so a missing module makes each test fail on its own
(ImportError) instead of breaking the collection of the whole file.

Conventions used by every test of the story:

* money is integer paise; an expected figure comes from an ORACLE written here, never from the
  code under test. The cost oracle is `CostModel.leg(...).total` (the contract's own source) turned
  into paise with Decimal and ROUND_HALF_UP, once per leg (`leg_paise`); a second, independent
  oracle (`pure_decimal_leg`) re-derives a leg from the rates in config/costs.yaml in pure Decimal
  and is used for the worked examples (golden values);
* the contract's sides: a long (stock or option) is BUY then SELL; a stock SHORT is SELL then BUY
  (STT on the entry leg); an index option is always long premium (BUY then SELL), whichever way
  the underlying is traded (`sides`);
* the discriminating stock is MRF at Rs 1,00,000: one share there turns over Rs 1 lakh, so STT
  (Rs 25) and stamp duty (Rs 3) are large against the 1 paisa rounding and a leg on the wrong
  side is visible in whole rupees. The cheap SBIN worked example of US-001/US-002 is kept for the
  golden values (fill 1000.30, TARGET 1020.00: costs 42 + 65 = 107 paise, gross 1970, net 1863);
* a leg total whose x100 is a half-paisa under one reading of "round the float" and not under the
  other (`1.005` is `1.00499...` in binary but `1.005` in its repr) is AMBIGUOUS: `leg_paise`
  raises `Tie` for it, a property test discards it (`reject_ties`), a fixed test must not use it;
* ledger tests insert CLOSED rows directly into `paper_trades` (the public schema), so they can
  state any sequence of closed trades, including sums far beyond 2**53.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import math
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable, Iterator, Sequence
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import us001_helpers as H
import us002_helpers as U
import yaml
from hypothesis import reject
from sqlalchemy import Engine, text

from onelot.costs import CostModel
from onelot.data.universe import InstrumentSpec
from onelot.store import init_database

ROOT = H.ROOT
TICK = U.TICK  # 5 paise
HUNDRED = Decimal(100)

MRF = InstrumentSpec("MRF", "MRF (1 share)", "STOCK", "MRF", 1, 0.05, "equity_intraday", 0.0)
SPECS: dict[str, InstrumentSpec] = {**H.SPECS, "MRF": MRF}

# paper_trades columns that hold money (integer paise)
PAISE_COLUMNS = (
    "entry_paise",
    "exit_paise",
    "gross_paise",
    "costs_paise",
    "net_paise",
    "risk_paise",
)
LEDGER_FIELDS = ("trades", "wins", "losses", "gross_paise", "costs_paise", "net_paise")


# ── the cost oracle ───────────────────────────────────────────────────────────────────
class Tie(Exception):
    """A leg total that sits on a half-paisa under one reading of float -> Decimal."""


def cost_model(product: str) -> CostModel:
    return CostModel(H._cost_cfg().product(product))


def model_for(key: str) -> CostModel:
    return cost_model(SPECS[key].product)


def paise_half_up(rupees: float) -> int:
    """rupees (a float) x 100, ROUND_HALF_UP, reading the float through its shortest repr."""
    return int((Decimal(repr(float(rupees))) * HUNDRED).quantize(Decimal(1), ROUND_HALF_UP))


def paise_half_up_binary(rupees: float) -> int:
    """The same, reading the float as its exact binary value (differs only on a tie)."""
    return int((Decimal(float(rupees)) * HUNDRED).quantize(Decimal(1), ROUND_HALF_UP))


def leg_paise(model: CostModel, price_paise: int, qty: int, side: str, tie_ok: bool = False) -> int:
    """round-half-up paise of CostModel.leg(price_rupees, qty, side).total: the contract's leg.
    `tie_ok` reads a tie through the shortest repr (the money.to_decimal convention) instead of
    raising `Tie`: for the older tests that re-use this oracle on a fixed scenario."""
    total = model.leg(price_paise / 100, qty, side).total  # type: ignore[arg-type]
    a, b = paise_half_up(total), paise_half_up_binary(total)
    if a != b and not tie_ok:
        raise Tie(f"leg total {total!r} is a tie between {a} and {b} paise")
    return a


def reject_ties(fn: Callable[..., Any], *args: Any) -> Any:
    """Call `fn`; a `Tie` discards the hypothesis example instead of failing the property."""
    try:
        return fn(*args)
    except Tie:
        reject()


def sides(kind: str, direction: str) -> tuple[str, str]:
    """(entry side, exit side): options are long premium; a stock short sells first."""
    if kind == "INDEX" or direction == "long":
        return "buy", "sell"
    return "sell", "buy"


def expected_costs(
    model: CostModel,
    kind: str,
    direction: str,
    entry_paise: int,
    exit_paise: int,
    qty: int,
    tie_ok: bool = False,
) -> int:
    """costs_paise: leg(entry, entry side) + leg(exit, exit side), each rounded once half-up."""
    first, second = sides(kind, direction)
    return leg_paise(model, entry_paise, qty, first, tie_ok) + leg_paise(
        model, exit_paise, qty, second, tie_ok
    )


def wrong_sides_costs(
    model: CostModel, direction: str, entry_paise: int, exit_paise: int, qty: int
) -> int:
    """What a book that assumed BUY then SELL for a short would book (the defect AC1 forbids)."""
    return leg_paise(model, entry_paise, qty, "buy") + leg_paise(model, exit_paise, qty, "sell")


def round_trip_gap(model: CostModel, entry_paise: int, exit_paise: int, qty: int) -> Decimal:
    """Exact (unrounded) round_trip_cost in paise: the number a long's costs_paise stays within
    1 paisa of."""
    return Decimal(repr(model.round_trip_cost(entry_paise / 100, exit_paise / 100, qty))) * HUNDRED


def pure_decimal_leg(product: str, price_paise: int, qty: int, side: str) -> Decimal:
    """One leg in rupees, re-derived from config/costs.yaml in PURE Decimal (no CostModel)."""
    table = yaml.safe_load((ROOT / "config" / "costs.yaml").read_text())["products"][product]
    rates, brokerage = table["taxes"], table["brokerage"]

    def d(x: Any) -> Decimal:
        return Decimal(str(x))

    turnover = Decimal(price_paise) * qty / HUNDRED
    if brokerage["mode"] == "flat":
        fee = d(brokerage["per_order_inr"])
    else:
        fee = min(
            d(brokerage["per_order_inr"]),
            d(brokerage["pct_of_turnover"]) * turnover,
            d(brokerage["cap_inr"]),
        )
    stt = d(rates["stt_sell_pct" if side == "sell" else "stt_buy_pct"]) * turnover
    exchange = d(rates["exchange_txn_pct"]) * turnover
    sebi = d(rates["sebi_turnover_pct"]) * turnover
    ipft = d(rates["ipft_pct"]) * turnover
    stamp = d(rates["stamp_duty_pct"]) * turnover if side == "buy" else Decimal(0)
    gst = d(rates["gst_pct"]) * (fee + exchange + sebi + ipft)
    return fee + stt + exchange + sebi + ipft + stamp + gst


def pure_decimal_costs(
    product: str, kind: str, direction: str, entry_paise: int, exit_paise: int, qty: int
) -> int:
    first, second = sides(kind, direction)
    return sum(
        int(
            (pure_decimal_leg(product, price, qty, side) * HUNDRED).quantize(
                Decimal(1), ROUND_HALF_UP
            )
        )
        for price, side in ((entry_paise, first), (exit_paise, second))
    )


def expected_gross(kind: str, direction: str, entry_paise: int, exit_paise: int, qty: int) -> int:
    """sign x (exit - entry) x qty; an option is long premium whatever the direction."""
    sign = 1 if kind == "INDEX" or direction == "long" else -1
    return sign * (exit_paise - entry_paise) * qty


def loud_cost_for(specs: dict[str, InstrumentSpec] | None = None) -> Callable[[str], CostModel]:
    """`cost_for` with exaggerated STT on the sell leg (1 %) and stamp duty on the buy leg
    (0.5 %): a leg booked on the wrong side is then wrong by whole rupees, at any price."""
    table = specs if specs is not None else SPECS

    def cost_for(instrument_key: str) -> CostModel:
        cfg = H._cost_cfg().product(table[instrument_key].product)
        taxes = cfg.taxes.model_copy(
            update={"stt_sell_pct": 0.01, "stt_buy_pct": 0.0, "stamp_duty_pct": 0.005}
        )
        return CostModel(cfg.model_copy(update={"taxes": taxes}))

    return cost_for


# ── a stand-in cost model that records how the book calls it ──────────────────────────
class RecordingCosts:
    """`cost_for` that records its calls and answers a chosen `total` per leg (rupees).

    `totals` maps a side to the total returned for it (default 0.125 buy / 0.625 sell: both
    exactly representable in binary, x100 = 12.5 and 62.5 paise, so half-up gives 13 + 63 = 76
    where half-even or truncation give 12 + 62 = 74 and rounding the SUM gives 75).
    """

    def __init__(self, totals: dict[str, float] | None = None) -> None:
        self.totals = totals or {"buy": 0.125, "sell": 0.625}
        self.asked: list[str] = []  # instrument keys cost_for was called with
        self.legs: list[tuple[float, int, str]] = []  # (price_rupees, qty, side)

    def cost_for(self, instrument_key: str) -> Any:
        self.asked.append(instrument_key)
        outer = self

        class Model:
            def leg(self, premium: float, qty: int, side: str) -> Any:
                outer.legs.append((premium, qty, side))
                return SimpleNamespace(total=outer.totals[side])

            def round_trip_cost(self, entry: float, exit_: float, qty: int) -> float:
                raise AssertionError("US-023 books each leg on its own side, not round_trip_cost")

        return Model()


# ── building the book ─────────────────────────────────────────────────────────────────
def make_book(
    engine: Engine,
    *,
    cost_for: Callable[[str], Any] | None = None,
    premium_at: Callable | None = None,
    specs: dict[str, InstrumentSpec] | None = None,
    bt: Any = None,
    enabled: bool = True,
    feed_delay: int = 15,
    clock: Callable[[], dt.datetime] | None = None,
) -> Any:
    """A PaperBook like `H.make_book`, but with an injectable `cost_for` and the MRF spec."""
    from onelot.config import PaperCfg

    specs = specs if specs is not None else SPECS
    return H.paper_book_class()(
        engine,
        PaperCfg(enabled=enabled, feed_delay_minutes=feed_delay),
        bt or H.BT,
        specs,
        cost_for or H.cost_for_specs(specs),
        premium_at or H.make_pricer(),
        clock=clock or (lambda: H.at(10, 30)),
    )


@contextlib.contextmanager
def temp_db() -> Iterator[Engine]:
    """A throw-away SQLite database (all tables) for one hypothesis example."""
    with tempfile.TemporaryDirectory() as folder:
        database = init_database(f"sqlite:///{folder}/us023.db")
        try:
            yield database.engine
        finally:
            database.dispose()


# ── stock scenarios, in paise ─────────────────────────────────────────────────────────
def stock_alert(
    alert_id: str = "A1",
    direction: str = "long",
    key: str = "SBIN",
    entry: float = 1000.0,
    **kw: Any,
) -> dict[str, Any]:
    """A stock alert at `entry` with the default 1 % stop and 2 % target (trail off)."""
    return U.make_alert(
        alert_id, direction=direction, instrument_key=key, series_key=key, entry=entry, **kw
    )


def stock_rows(alert: dict[str, Any], hit: str) -> list[U.Row]:
    """The fill bar (opens at the alert's entry, touches nothing) and one bar that ends the
    trade: hit 'target' (a limit fill at the alert's target), 'stop' (the stop, slipped) or
    'quiet' (nothing: the trade stays OPEN)."""
    eco = alert["economics"]
    e = H.to_paise(eco["underlying_entry"])
    stop, target = H.to_paise(eco["underlying_stop"]), H.to_paise(eco["underlying_target"])
    long_ = alert["direction"] == "long"
    fill: U.Row = (e, e + 10, e - 10, e)
    if hit == "quiet":
        return [fill, fill]
    if hit == "target":
        wick = target + 10 if long_ else target - 10
        end: U.Row = (e, wick, e - 10, target) if long_ else (e, e + 10, wick, target)
        return [fill, end]
    wick = stop - 10 if long_ else stop + 10
    end = (e, e + 10, wick, stop) if long_ else (e, wick, e - 10, stop)
    return [fill, end]


def run_stock(
    engine: Engine,
    *,
    direction: str = "long",
    key: str = "SBIN",
    entry: float = 1000.0,
    hit: str = "target",
    alert_id: str = "A1",
    book: Any = None,
    alert_kw: dict[str, Any] | None = None,
    **book_kw: Any,
) -> tuple[Any, dict[str, Any], list[Any]]:
    """Open one stock trade, fill it and drive it to `hit`. Returns (book, row, events)."""
    alert = stock_alert(alert_id, direction, key, entry, **(alert_kw or {}))
    book = book if book is not None else make_book(engine, **book_kw)
    H.open_trade(book, alert)
    frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *stock_rows(alert, hit)])
    events = U.step_book(book, frame, series=key)
    return book, U.row_of(engine, alert_id), events


# ── option scenarios (underlying levels in paise; the fake pricer prices the option) ──
OPT_PRE = (5399000, 5399500, 5398500, 5399000)
CE_FILL = (5400000, 5401000, 5399000, 5400500)
PE_FILL = (5400000, 5400500, 5399500, 5399800)
OPTION_SCENARIOS: dict[str, tuple[str, list[U.Row], str, float]] = {
    # id: (direction, rows after the pre-alert bar, expected close reason, underlying exit level)
    "ce_target": ("long", [CE_FILL, (5400500, 5431000, 5400000, 5430000)], "TARGET", 54300.0),
    "ce_stop": ("long", [CE_FILL, (5400500, 5400600, 5384000, 5390000)], "STOP", 53850.0),
    "ce_gap_stop": ("long", [CE_FILL, (5370000, 5371000, 5369000, 5370500)], "STOP", 53700.0),
    "pe_target": (
        "short",
        [PE_FILL, (5399800, 5399900, 5369000, 5370500)],
        "TARGET",
        53700.0,
    ),
    "pe_stop": ("short", [PE_FILL, (5399800, 5416000, 5399000, 5415500)], "STOP", 54150.0),
}


def run_option(
    engine: Engine,
    rows: Sequence[U.Row],
    *,
    direction: str = "long",
    alert: dict[str, Any] | None = None,
    alert_id: str = "O1",
    pricer: Callable | None = None,
    book: Any = None,
    now: dt.datetime | None = None,
    **book_kw: Any,
) -> tuple[Any, dict[str, Any], list[Any]]:
    """Open one BANKNIFTY option trade and step `rows` (after the 10:05 pre-alert bar)."""
    alert = alert or U.make_alert(alert_id, kind="INDEX", direction=direction)
    if book is None:
        book = make_book(engine, premium_at=pricer or H.make_pricer(), **book_kw)
    H.open_trade(book, alert)
    frame = U.pbars(H.at(10, 5), [OPT_PRE, *rows])
    events = U.step_book(book, frame, now, series=alert["series_key"])
    return book, U.row_of(engine, alert["id"]), events


def chain_alert(
    alert_id: str = "O1", direction: str = "long", chain: float = 455.25, **kw: Any
) -> dict:
    """A LIVE-CHAIN option alert: premium 455.25 against the fake pricer's 450.00 at the alert,
    so the recorded basis is exactly +525 paise (`plan["pricing"]["basis_paise"]`)."""
    alert = U.make_alert(alert_id, kind="INDEX", direction=direction, premium=chain, **kw)
    alert["economics"]["synthetic"] = False
    return alert


def position_of(engine: Engine, alert_id: str = "O1") -> dict[str, Any]:
    return dict(U.row_of(engine, alert_id)["plan"]["position"])


def mark_of(engine: Engine, alert_id: str = "O1") -> Any:
    return position_of(engine, alert_id).get("mark_paise")


def fake_mark(
    option_alert: dict[str, Any],
    underlying_paise: int,
    basis_paise: int = 0,
    pricer: Callable | None = None,
) -> int:
    """The mark the contract asks for: max(to_paise(premium_at(option, underlying)) + basis,
    one tick) (Manager decision 4): never below 5 paise, never 0."""
    option = dict(option_alert["economics"]["option"])
    fair = (pricer or H.make_pricer())(option, underlying_paise / 100, H.at(10, 7))
    return max(H.to_paise(fair) + basis_paise, TICK)


def shifted(pricer: Callable, rupees: float) -> Callable:
    """`pricer` plus a constant: what a live-chain trade's premium is, model + basis, in rupees.
    Handed to the Lab (which knows no basis), it makes `execute_trade` the oracle of a chain
    trade's entry, exit and gross (Manager decision 1: the basis applies EVERYWHERE)."""

    def premium_at(option: Any, underlying: float, ts: Any) -> float:
        return pricer(option, underlying, ts) + rupees

    return premium_at


def pricing_events(engine: Engine, alert_id: str = "O1", leg: str | None = None) -> list[dict]:
    found = [
        e for e in H.event_rows(engine, alert_id) if str(H.val(e["kind"])) == "PRICING_UNAVAILABLE"
    ]
    if leg is not None:
        found = [e for e in found if (e["detail"] or {}).get("leg") == leg]
    return found


class SwitchablePricer:
    """The fake pricer while `failing` is False; once a test sets it, a chosen bad answer (or
    an exception). The switch is the test's, never the clock: which timestamp the book prices a
    mark at is the book's business."""

    def __init__(self, bad: Any = None) -> None:
        self.good = H.make_pricer()
        self.bad = bad
        self.failing = False
        self.calls: list[tuple[dict, float, Any]] = []

    def __call__(self, option: Any, underlying: float, ts: Any) -> Any:
        self.calls.append((dict(option), float(underlying), ts))
        if self.failing:
            if isinstance(self.bad, Exception):
                raise self.bad
            return self.bad
        return self.good(option, underlying, ts)


# ── many closed trades in one database ────────────────────────────────────────────────
MANY = (
    # (alert id, kind, direction, series key, stock hit | option scenario)
    ("S1", "STOCK", "long", "SBIN", "target"),
    ("S2", "STOCK", "long", "RELIANCE", "stop"),
    ("S3", "STOCK", "short", "MRF", "target"),
    ("S4", "STOCK", "short", "HDFCBANK", "stop"),
    ("O1", "INDEX", "long", "BANKNIFTY", "ce_target"),
    ("O2", "INDEX", "short", "NIFTY", "pe_stop"),
)


def close_many(engine: Engine) -> tuple[Any, list[dict[str, Any]]]:
    """Six trades (stock long/short, option CE/PE; three winners, three losers) on one book and
    one database, all closed by one `step`. Returns (book, their rows in MANY order)."""
    book = make_book(engine)
    frames = {}
    for alert_id, kind, direction, key, hit in MANY:
        if kind == "STOCK":
            alert = stock_alert(alert_id, direction, key, 100000.0 if key == "MRF" else 1000.0)
            frames[key] = U.pbars(H.at(10, 5), [U.PRE_BAR, *stock_rows(alert, hit)])
        else:
            instrument = "NIFTY_OPT" if key == "NIFTY" else "BANKNIFTY_OPT"
            alert = U.make_alert(
                alert_id, kind="INDEX", direction=direction, instrument_key=instrument
            )
            frames[key] = U.pbars(H.at(10, 5), [OPT_PRE, *OPTION_SCENARIOS[hit][1]])
        H.open_trade(book, alert)
    book.step(H.at(10, 9), H.Feed(**frames))
    return book, [U.row_of(engine, m[0]) for m in MANY]


def money_of(row: dict[str, Any]) -> tuple[int, int, int]:
    """(gross, costs, net) the contract gives a CLOSED priced row, from the oracle alone."""
    entry, exit_, qty = row["entry_paise"], row["exit_paise"], row["qty"]
    gross = expected_gross(row["kind"], row["direction"], entry, exit_, qty)
    costs = expected_costs(
        model_for(row["instrument_key"]), row["kind"], row["direction"], entry, exit_, qty
    )
    return gross, costs, gross - costs


# ── closes without money, and rows that never filled ──────────────────────────────────
def unpriced_stock_close(engine: Engine, alert_id: str = "U1", key: str = "SBIN") -> Any:
    """A stock trade that fills on the open, never sees a usable bar, and is closed SESSION_END
    at 15:30 with NO price (exit and gross NULL): the 'unpriced exit' of a stock."""
    book = make_book(engine)
    H.open_trade(book, stock_alert(alert_id, key=key))
    frame = H.minute_bars(H.at(10, 5), [1000.0, 1000.0])
    frame["high"] = frame["low"] = frame["close"] = float("nan")
    book.step(H.at(10, 8), H.Feed(**{key: frame}))
    book.step(H.at(15, 30), H.Feed())
    return book


def no_fill_gap(engine: Engine, alert_id: str = "G1", key: str = "HDFCBANK", **book_kw: Any) -> Any:
    """NO_FILL GAP_THROUGH_STOP: the first bar opens beyond the stop of a long."""
    book = make_book(engine, **book_kw)
    H.open_trade(book, stock_alert(alert_id, key=key))
    book.step(H.at(10, 9), H.Feed(**{key: H.minute_bars(H.at(10, 5), [1000.0, 980.0, 980.0])}))
    return book


def no_fill_expired(engine: Engine, alert_id: str = "E1", key: str = "INFY") -> Any:
    """NO_FILL expired: the only bars open after the alert's 10-minute window."""
    book = make_book(engine)
    H.open_trade(book, stock_alert(alert_id, key=key))
    book.step(H.at(10, 25), H.Feed(**{key: H.minute_bars(H.at(10, 20), [1000.0, 1000.0])}))
    return book


def no_fill_session_end(engine: Engine, alert_id: str = "X1", key: str = "SBIN") -> Any:
    """NO_FILL session_end: the first bar after the alert opens at the 15:15 exit time."""
    book = make_book(engine)
    created = H.at(15, 14, 3)
    H.open_trade(book, stock_alert(alert_id, key=key, created=created), now=created)
    frame = H.minute_bars(H.at(15, 14), [1000.0, 1000.0, 1000.0])
    book.step(H.at(15, 18), H.Feed(**{key: frame}))
    return book


NO_FILLS = {"gap": no_fill_gap, "expired": no_fill_expired, "session_end": no_fill_session_end}
STATUS_BASE = frozenset({"opened", "filled", "no_fill", "closed", "hook_errors"})


def counted_problems(book: Any) -> int:
    """How many problems the book says it counted: its hook_errors plus any counter it added."""
    status = book.status()
    return int(status.get("hook_errors", 0)) + sum(
        int(v) for k, v in status.items() if k not in STATUS_BASE
    )


def null_out_costs(engine: Engine) -> None:
    """Make every CLOSED row look as US-002 wrote it: costs and net NULL."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE paper_trades SET costs_paise = NULL, net_paise = NULL WHERE status='CLOSED'"
            )
        )


def rows_without(engine: Engine, *ignore: str) -> list[dict[str, Any]]:
    """Every paper_trades row (by id), minus the columns in `ignore`."""
    return [{k: v for k, v in r.items() if k not in ignore} for r in H.trade_rows(engine)]


# ── the Lab as the differential oracle for an option round trip ───────────────────────
def lab_option_row(
    *,
    bt: Any,
    spec: Any,
    opt_type: str,
    underlying_entry: float,
    underlying_exit: float,
    entry_ts: dt.datetime,
    exit_ts: dt.datetime,
    pricer: Callable[[Any, float, Any], float | None],
    stop: float,
) -> dict[str, Any]:
    """The whole row `lab.execution.execute_trade` books for a long option bought at
    `underlying_entry` and sold at `underlying_exit` (one full leg), with a fake pricer."""
    import pandas as pd

    from onelot.lab.execution import execute_trade
    from onelot.lab.simulator import Leg, SimTrade

    class Pricer:
        def iv_for(self, ts):
            return 0.16

        def premium(self, spot, k, ts, expiry, opt):
            return pricer({"opt_type": opt}, spot, ts)

        def delta(self, spot, k, ts, expiry, opt):
            return 0.65

    sign = 1 if opt_type == "CE" else -1
    t = SimTrade(
        symbol="BANKNIFTY",
        strategy="ema_cross",
        side=sign,
        signal_ts=pd.Timestamp(entry_ts) - pd.Timedelta(minutes=1),
        entry_ts=pd.Timestamp(entry_ts),
        entry_price=float(underlying_entry),
        stop_initial=float(stop),
        risk_ps=abs(float(underlying_entry) - float(stop)),
        target=None,
        strength=60.0,
        atr=100.0,
        legs=[Leg(pd.Timestamp(exit_ts), float(underlying_exit), 1.0, "test")],
    )
    return execute_trade(
        t, spec, CostModel(H._cost_cfg().product(spec.product)), bt, bt.slippage_pct, Pricer()
    )


def option_entry_oracle(fair: Any, spread: float, ticks: int, tick: float = 0.05) -> int:
    """round_to_tick UP of fair x (1 + spread) + ticks x tick: the premium the book pays for an
    option whose premium (model + basis) is `fair` rupees, in exact rational arithmetic, in paise.
    `fair` may be a number or a Fraction."""
    tick_p = Fraction(str(tick)) * 100
    value = Fraction(str(fair)) * (1 + Fraction(str(spread))) * 100 + ticks * tick_p
    return int(math.ceil(value / tick_p) * tick_p)


def option_exit_oracle(fair: float, spread: float, ticks: int, tick: float = 0.05) -> int:
    """round_to_tick DOWN of fair x (1 - spread) - ticks x tick, floored at one tick: in exact
    rational arithmetic on the decimal inputs (the story's close fill, AC3), in paise."""
    tick_p = Fraction(str(tick)) * 100
    value = Fraction(str(fair)) * (1 - Fraction(str(spread))) * 100 - ticks * tick_p
    down = math.floor(value / tick_p) * tick_p
    return int(max(down, tick_p))


# ── ledger rows, inserted straight into paper_trades ──────────────────────────────────
IST_DAY = dt.date(2026, 10, 28)


class Booked(NamedTuple):
    """One trade as the ledger's oracle sees it."""

    alert_id: str
    status: str
    gross: int | None
    costs: int | None
    net: int | None
    grade: str
    ts: dt.datetime  # created_at = entry_ts = exit_ts = updated_at: any filter field agrees


def booked(
    alert_id: str,
    gross: int,
    costs: int,
    *,
    ts: dt.datetime,
    grade: str = "live",
    status: str = "CLOSED",
    net: int | None | str = "derive",
) -> Booked:
    n = gross - costs if net == "derive" else net
    return Booked(alert_id, status, gross, costs, n, grade, ts)  # type: ignore[arg-type]


def insert_booked(engine: Engine, items: Sequence[Booked]) -> None:
    """Write `items` as paper_trades rows, in the order given (so ids follow that order)."""
    from onelot.store.schema import paper_trades

    rows = []
    for b in items:
        entry = 10_000_000
        exit_paise = None if b.gross is None else entry + b.gross
        rows.append(
            {
                "alert_id": b.alert_id,
                "status": b.status,
                "close_reason": {"CLOSED": "TARGET", "NO_FILL": "NO_FILL"}.get(b.status),
                "grade": b.grade,
                "data_source": "dhan" if b.grade == "live" else "yahoo",
                "instrument_key": "SBIN",
                "series_key": "SBIN",
                "kind": "STOCK",
                "side": "BUY",
                "direction": "long",
                "strategy": "ema_cross",
                "timeframe": "10m",
                "confidence": 61.5,
                "product": "equity_intraday",
                "qty": 1,
                "entry_ts": b.ts if b.status in ("OPEN", "CLOSED") else None,
                "fill_bar_ts": b.ts if b.status in ("OPEN", "CLOSED") else None,
                "exit_ts": b.ts if b.status == "CLOSED" else None,
                "last_bar_ts": b.ts if b.status in ("OPEN", "CLOSED") else None,
                "resolution_min": 1,
                "entry_paise": entry if b.status in ("OPEN", "CLOSED") else None,
                "exit_paise": exit_paise if b.status == "CLOSED" else None,
                "gross_paise": b.gross,
                "costs_paise": b.costs,
                "net_paise": b.net,
                "risk_paise": 1030 if b.status in ("OPEN", "CLOSED") else None,
                "created_at": b.ts,
                "expires_at": b.ts + dt.timedelta(minutes=10),
                "plan": {"alert": {}, "levels": {}},
                "updated_at": b.ts,
            }
        )
    with engine.begin() as conn:
        for row in rows:
            conn.execute(paper_trades.insert().values(**row))


def oracle_totals(
    items: Sequence[Booked],
    *,
    grade: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> dict[str, int]:
    """PaperLedger.totals by definition: CLOSED rows with a net, optionally in a grade and a
    half-open window [since, until), summed in Decimal. wins: net > 0; losses: net < 0."""
    chosen = [
        b
        for b in items
        if b.status == "CLOSED"
        and b.net is not None
        and (grade is None or b.grade == grade)
        and (since is None or b.ts >= since)
        and (until is None or b.ts < until)
    ]

    def total(values: Sequence[int | None]) -> int:
        return int(sum((Decimal(v) for v in values if v is not None), Decimal(0)))

    return {
        "trades": len(chosen),
        "wins": sum(1 for b in chosen if b.net is not None and b.net > 0),
        "losses": sum(1 for b in chosen if b.net is not None and b.net < 0),
        "gross_paise": total([b.gross for b in chosen]),
        "costs_paise": total([b.costs for b in chosen]),
        "net_paise": total([b.net for b in chosen]),
    }


def ledger_module() -> Any:
    return importlib.import_module("onelot.paper.ledger")


def make_ledger(engine: Engine) -> Any:
    return ledger_module().PaperLedger(engine)


def as_dict(totals: Any) -> dict[str, Any]:
    return {name: getattr(totals, name) for name in LEDGER_FIELDS}


def typeofs(engine: Engine, column: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT typeof({column}) FROM paper_trades ORDER BY id"))
        return [str(r[0]) for r in rows]


# ── GET /api/paper (US-030) ───────────────────────────────────────────────────────────
def paper_api(book: Any) -> dict[str, Any]:
    """GET /api/paper over the real app factory with a stub engine whose desk carries `book`."""
    from unittest.mock import MagicMock

    from onelot.api.server import create_app

    with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    engine = MagicMock()
    engine.desk = SimpleNamespace(paper=book, hook_errors=0)
    client = TestClient(create_app(engine, "test-token"), raise_server_exceptions=False)
    response = client.get("/api/paper")
    assert response.status_code == 200, (
        f"GET /api/paper answered {response.status_code}: US-030 (story/US-030-paper-view) "
        "must be merged into this branch for the Paper panel's API, then US-023 adds the "
        "mark and unrealised fields to it"
    )
    return dict(response.json())


def api_trade(book: Any, alert_id: str) -> dict[str, Any]:
    found = [t for t in paper_api(book)["trades"] if t["alert_id"] == alert_id]
    assert len(found) == 1, f"/api/paper must list trade {alert_id} exactly once, got {found}"
    return dict(found[0])


def rupees(paise: int) -> str:
    """Integer paise -> the rupee string /api/paper uses: two decimals, exact."""
    return format((Decimal(paise) / HUNDRED).quantize(Decimal("0.01")), "f")


# ── AC4: counting how often the cost table is parsed ──────────────────────────────────
@contextlib.contextmanager
def count_cost_table_parses() -> Iterator[dict[str, int]]:
    """Counts parses of the cost table in THIS process, however they are made: through
    CostConfig.load or through yaml.safe_load producing a mapping that looks like costs.yaml."""
    from onelot.config import CostConfig

    counts = {"load": 0, "yaml": 0}
    real_safe_load = yaml.safe_load
    real_load = CostConfig.load.__func__  # type: ignore[attr-defined]

    def counting_load(cls: Any, path: Any) -> Any:
        if Path(str(path)).name == "costs.yaml":
            counts["load"] += 1
        return real_load(cls, path)

    def counting_safe_load(stream: Any) -> Any:
        result = real_safe_load(stream)
        if isinstance(result, dict) and {"brokerage", "taxes", "effective_from"} <= set(result):
            counts["yaml"] += 1
        return result

    CostConfig.load = classmethod(counting_load)  # type: ignore[method-assign,assignment]
    yaml.safe_load = counting_safe_load  # type: ignore[assignment]
    try:
        yield counts
    finally:
        CostConfig.load = classmethod(real_load)  # type: ignore[method-assign]
        yaml.safe_load = real_safe_load  # type: ignore[assignment]


COLD_PROCESS_SCRIPT = """
import json, sys, yaml
from pathlib import Path
import onelot.config as config

counts = {"load": 0, "yaml": 0}
real_safe_load, real_load = yaml.safe_load, config.CostConfig.load.__func__

def counting_load(cls, path):
    if Path(str(path)).name == "costs.yaml":
        counts["load"] += 1
    return real_load(cls, path)

def counting_safe_load(stream):
    result = real_safe_load(stream)
    if isinstance(result, dict) and {"brokerage", "taxes", "effective_from"} <= set(result):
        counts["yaml"] += 1
    return result

config.CostConfig.load = classmethod(counting_load)
yaml.safe_load = counting_safe_load

from onelot.desk import economics
from onelot.data.universe import InstrumentSpec

stock = InstrumentSpec("SBIN", "SBIN", "STOCK", "SBIN", 1, 0.05, "equity_intraday", 0.0)
option = InstrumentSpec("BANKNIFTY_OPT", "BN", "OPTIONS", "BANKNIFTY", 30, 0.05, "index_options", 100.0)
seen = []
for i in range(40):
    model = economics._costs_for(stock if i % 2 == 0 else option)
    seen.append([model.cfg.taxes.stt_sell_pct, model.cfg.brokerage.mode])
print(json.dumps({"counts": counts, "seen": seen}))
"""


def cold_process_parses() -> dict[str, Any]:
    """Run 40 `_costs_for` calls (two products) in a FRESH interpreter; report the parses."""
    done = subprocess.run(
        [sys.executable, "-c", COLD_PROCESS_SCRIPT],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin", "HOME": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, f"cold process failed:\n{done.stderr[-2000:]}"
    return dict(json.loads(done.stdout.strip().splitlines()[-1]))
