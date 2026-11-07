"""What a one-unit fill costs, measured from the broker's quotes (`onelot.desk.fillcost`).

Read-only: every test runs on a fake adapter that only answers `quote`, `expiry_list_raw` and
`option_chain`, never places anything, and the database is a SQLite file in tmp_path.

    recorder   ALERT quote at the alert, FOLLOW quotes +60 / +120 / +300 s, SAMPLE quotes only
               while the market is open and every `sample_every` s, option ids from the chain
    report     median / p90 half-spread per instrument and time of day, an alert's drift signed
               so positive = worse for its side, the owner's own fills against the alert's
    charges    Dhan's billed charges per fill against the cost table, per (product, side)
    surfaces   the `fill_quotes` table, GET /api/desk/fill-costs, `python -m onelot fill-costs`
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from desk_harness import new_desk, small_cfg
from sqlalchemy import create_engine, insert, inspect, select

from onelot.api.server import create_app
from onelot.broker.base import ChainRow, ChainSnapshot
from onelot.clock import IST
from onelot.config import AppConfig, BacktestCfg, CostConfig
from onelot.costs import CostModel
from onelot.desk.fillcost import (
    BUCKETS,
    CHARGE_FIELDS,
    FOLLOW_OFFSETS,
    FillCostRecorder,
    Watched,
    adverse_pct,
    charges_check,
    cost_product,
    exec_price,
    half_spread_pct,
    report,
)
from onelot.store import init_database
from onelot.store.schema import METADATA, desk_positions, desk_signals, fill_quotes

with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 11, 6, 10, 0, tzinfo=IST)  # a Friday morning
LISTED = ["2026-11-10", "2026-11-17", "2026-11-24", "2026-12-01", "2026-12-29"]
STOCK = Watched("RELIANCE", "2885", "NSE_EQ", "RELIANCE", "EQUITY")
OTHER_STOCK = Watched("SBIN", "3045", "NSE_EQ", "SBIN", "EQUITY")
INDICES = [("BANKNIFTY", "25", "NSE", "BANKNIFTY_OPT")]


def q(ltp=None, bid=None, ask=None, bid_qty=100, ask_qty=120):
    return SimpleNamespace(ltp=ltp, bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty)


def rows_of(engine, table=fill_quotes) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings().all()]


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    database = init_database(f"sqlite:///{tmp_path / 'fills.db'}")
    use_database(database)
    yield database
    database.dispose()
    get_database.__globals__["_db"] = None


@pytest.fixture
def engine(db):
    return db.engine


# ══ the small pure helpers ════════════════════════════════════════════════════════════════


class TestHalfSpread:
    @pytest.mark.parametrize(
        ("bid", "ask", "want"),
        [
            (99.0, 101.0, 1.0),  # 1 on each side of a mid of 100
            (100.0, 100.0, 0.0),  # a locked market costs nothing but is real
            (99.95, 100.05, 0.05),
            (1000.0, 1001.0, 0.5 / 1000.5 * 100),
            (344.0, 346.0, 1.0 / 345.0 * 100),
            (0.05, 0.10, 0.025 / 0.075 * 100),
        ],
    )
    def test_half_the_quoted_spread_as_a_percent_of_the_mid(self, bid, ask, want):
        assert half_spread_pct(bid, ask) == pytest.approx(want)

    @pytest.mark.parametrize(
        ("bid", "ask"),
        [
            (None, 101.0), (99.0, None), (None, None), (0.0, 101.0), (99.0, 0.0), (-1.0, 101.0),
            (101.0, 99.0),  # crossed
            (float("nan"), 101.0), (99.0, float("nan")), (99.0, float("inf")), ("99", "101"),
        ],
    )  # fmt: skip
    def test_a_missing_one_sided_non_finite_or_crossed_quote_is_none(self, bid, ask):
        assert half_spread_pct(bid, ask) is None


class TestExecPrice:
    @pytest.mark.parametrize("side", ["BUY", "BUY_CE", "BUY_PE"])
    def test_a_buy_pays_the_ask(self, side):
        assert exec_price(side, 99.0, 101.0, 100.0) == 101.0

    def test_a_sell_gets_the_bid(self):
        assert exec_price("SELL", 99.0, 101.0, 100.0) == 99.0

    @pytest.mark.parametrize("empty", [None, 0.0, -1.0, float("nan")])
    def test_an_empty_side_of_the_book_falls_back_to_the_last_price(self, empty):
        assert exec_price("BUY", 99.0, empty, 100.0) == 100.0
        assert exec_price("SELL", empty, 101.0, 100.0) == 100.0

    def test_nothing_at_all_is_none(self):
        assert exec_price("BUY", None, None, None) is None
        assert exec_price("SELL", None, None, 0.0) is None

    def test_the_answer_is_a_float(self):
        assert type(exec_price("BUY", 99, 101, 100)) is float


class TestAdversePct:
    @pytest.mark.parametrize(
        ("side", "base", "later", "want"),
        [
            ("BUY", 100.0, 101.0, 1.0),  # paid more: worse
            ("BUY", 100.0, 99.0, -1.0),
            ("BUY_CE", 200.0, 202.0, 1.0),
            ("SELL", 100.0, 101.0, -1.0),  # sold higher: better
            ("SELL", 100.0, 99.0, 1.0),  # sold lower: worse
            ("BUY", 100.0, 100.0, 0.0),
        ],
    )
    def test_positive_is_worse_for_that_side(self, side, base, later, want):
        assert adverse_pct(side, base, later) == pytest.approx(want)


# ══ the recorder ══════════════════════════════════════════════════════════════════════════


class FakeAdapter:
    """quote / expiry_list_raw / option_chain, with recorders. Nothing here can trade."""

    def __init__(self, quotes=None, expiries=None, chains=None, fail_quote=False):
        self.quotes: dict[str, Any] = dict(quotes or {})
        self.expiries = LISTED if expiries is None else expiries
        self.chains: dict[str, list[ChainRow] | Exception] = dict(chains or {})
        self.fail_quote = fail_quote
        self.quote_calls: list[list] = []
        self.expiry_calls: list[tuple] = []
        self.chain_calls: list[tuple] = []

    def quote(self, contracts):
        self.quote_calls.append(list(contracts))
        if self.fail_quote:
            raise RuntimeError("DH-904 rate limit")
        return {str(c.security_id): self.quotes[str(c.security_id)] for c in contracts
                if str(c.security_id) in self.quotes}  # fmt: skip

    def expiry_list_raw(self, security_id, segment="IDX_I"):
        self.expiry_calls.append((security_id, segment))
        return list(self.expiries)

    def option_chain(self, underlying, expiry, security_id=25, segment="IDX_I"):
        self.chain_calls.append((underlying, expiry, security_id, segment))
        got = self.chains.get(underlying, [])
        if isinstance(got, Exception):
            raise got
        return ChainSnapshot(NOW, underlying, expiry, 45210.0, list(got))


def chain_rows() -> list[ChainRow]:
    return [
        ChainRow(44800.0, "CE", "70001", delta=0.82),
        ChainRow(45000.0, "CE", "70002", delta=0.66),  # the call nearest 0.65
        ChainRow(45200.0, "CE", "70003", delta=0.51),
        ChainRow(45400.0, "PE", "70011", delta=-0.30),
        ChainRow(45500.0, "PE", "70012", delta=-0.64),  # the put nearest 0.65 (|delta|)
        ChainRow(45700.0, "PE", "70013", delta=-0.80),
        ChainRow(45100.0, "CE", None, delta=0.65),  # no id: unusable
        ChainRow(45150.0, "PE", "70099", delta=None),  # no delta: unusable
    ]


class World:
    """A recorder wired to fakes, with a market that opens and shuts on demand."""

    def __init__(self, engine, adapter, **kw):
        self.adapter = adapter
        self.open = False
        self.active = True
        self.stocks = [STOCK]
        self.indices = list(INDICES)
        args = {"sample_every": 300, "chain_every": 1800}
        args.update(kw)
        self.rec = FillCostRecorder(
            engine,
            lambda: self.adapter,
            lambda: self.active,
            lambda now: self.open,
            lambda: list(self.stocks),
            lambda: list(self.indices),
            **args,
        )
        self.engine = engine

    def tick(self, now: dt.datetime) -> int:
        return self.rec.tick(now)

    @property
    def rows(self) -> list[dict]:
        return rows_of(self.engine)


def stock_alert(alert_id="aaaa11112222", side="BUY", instrument="RELIANCE", price=1000.0):
    return {
        "id": alert_id, "instrument_key": instrument, "series_key": instrument, "side": side,
        "economics": {"entry_price": price, "option": {}},
    }  # fmt: skip


def option_alert(alert_id="bbbb11112222", side="BUY_CE", sid="70002", segment=None, strike=45000.0):
    opt = {"security_id": sid, "strike": strike, "opt_type": "CE", "expiry": "2026-11-24"}
    if segment:
        opt["segment"] = segment
    return {
        "id": alert_id, "instrument_key": "BANKNIFTY_OPT", "series_key": "BANKNIFTY", "side": side,
        "economics": {"entry_price": 345.0, "option": opt},
    }  # fmt: skip


@pytest.fixture
def world(engine) -> World:
    adapter = FakeAdapter(
        quotes={
            "2885": q(1000.0, 999.9, 1000.1),
            "3045": q(800.0, 799.9, 800.1),
            "70002": q(345.0, 344.0, 346.0),
            "70012": q(380.0, 379.0, 381.0),
        },
        chains={"BANKNIFTY": chain_rows()},
    )
    return World(engine, adapter)


class TestNothingWithoutADataPlan:
    def test_without_an_adapter_nothing_is_quoted_or_written(self, world):
        world.adapter_was = world.adapter
        world.adapter = None
        world.open = True
        world.rec.on_alert(stock_alert())
        assert world.tick(NOW) == 0
        assert world.rows == []
        assert world.adapter_was.quote_calls == []

    def test_with_the_data_api_inactive_nothing_is_quoted_or_written(self, world):
        world.active = False
        world.open = True
        world.rec.on_alert(stock_alert())
        assert world.tick(NOW) == 0
        assert world.rows == [] and world.adapter.quote_calls == []
        assert world.adapter.expiry_calls == [] and world.adapter.chain_calls == []

    def test_with_nothing_to_ask_the_broker_is_not_called(self, world):
        assert world.tick(NOW) == 0  # market shut, no alerts
        assert world.adapter.quote_calls == []


class TestTheAlertQuote:
    def test_an_alert_is_quoted_at_once_and_the_row_carries_all_of_it(self, world):
        world.rec.on_alert(stock_alert())
        assert world.tick(NOW) == 1
        (row,) = world.rows
        assert row["kind"] == "ALERT" and row["ts"] == NOW.astimezone(dt.timezone.utc)
        assert (row["instrument_key"], row["security_id"], row["segment"]) == (
            "RELIANCE", "2885", "NSE_EQ",
        )  # fmt: skip
        assert (row["alert_id"], row["side"], row["offset_sec"]) == ("aaaa11112222", "BUY", None)
        assert (row["ltp"], row["bid"], row["ask"]) == (1000.0, 999.9, 1000.1)
        assert (row["bid_qty"], row["ask_qty"]) == (100, 120)
        assert row["label"] == "RELIANCE"

    def test_the_request_names_the_instrument_it_quotes(self, world):
        world.rec.on_alert(stock_alert())
        world.tick(NOW)
        ((contract,),) = world.adapter.quote_calls
        assert (contract.security_id, contract.exchange_segment) == ("2885", "NSE_EQ")
        assert contract.instrument_type == "EQUITY" and contract.underlying == "RELIANCE"

    def test_an_alert_is_quoted_once_not_on_every_tick(self, world):
        world.rec.on_alert(stock_alert())
        world.tick(NOW)
        assert world.tick(NOW + dt.timedelta(seconds=10)) == 0
        assert len(world.adapter.quote_calls) == 1

    def test_the_alert_is_copied_so_later_changes_to_it_do_not_reach_the_row(self, world):
        sig = stock_alert()
        world.rec.on_alert(sig)
        sig["side"] = "SELL"
        sig["id"] = "changed"
        world.tick(NOW)
        assert (world.rows[0]["side"], world.rows[0]["alert_id"]) == ("BUY", "aaaa11112222")

    def test_an_option_alert_quotes_the_contract_it_names(self, world):
        world.rec.on_alert(option_alert())
        world.tick(NOW)
        (row,) = world.rows
        assert (row["security_id"], row["segment"], row["side"]) == ("70002", "NSE_FNO", "BUY_CE")
        assert row["instrument_key"] == "BANKNIFTY_OPT"
        assert row["label"] == "BANKNIFTY 45000 CE 2026-11-24"
        ((contract,),) = world.adapter.quote_calls
        assert contract.instrument_type == "OPTIDX" and contract.exchange_segment == "NSE_FNO"

    def test_a_bse_option_names_its_own_segment(self, world):
        world.adapter.quotes["70002"] = q(345.0, 344.0, 346.0)
        world.rec.on_alert(option_alert(segment="BSE_FNO"))
        world.tick(NOW)
        assert world.rows[0]["segment"] == "BSE_FNO"

    def test_a_synthetic_option_has_no_contract_to_quote(self, world):
        sig = option_alert(sid=None)  # type: ignore[arg-type]
        sig["economics"]["option"].pop("security_id")
        world.rec.on_alert(sig)
        assert world.tick(NOW) == 0
        assert world.adapter.quote_calls == []
        world.tick(NOW + dt.timedelta(seconds=400))  # and no follow-ups either
        assert world.rows == []

    def test_a_stock_that_is_not_watched_is_not_quoted(self, world):
        world.rec.on_alert(stock_alert(instrument="TCS"))
        assert world.tick(NOW) == 0 and world.adapter.quote_calls == []

    def test_each_watched_stock_alert_finds_its_own_instrument(self, world):
        world.stocks = [STOCK, OTHER_STOCK]
        world.rec.on_alert(stock_alert("s1", instrument="SBIN"))
        world.tick(NOW)
        assert world.rows[0]["security_id"] == "3045"

    def test_an_alert_without_a_quote_in_the_answer_writes_no_row(self, world):
        world.adapter.quotes.pop("2885")
        world.rec.on_alert(stock_alert())
        assert world.tick(NOW) == 0 and world.rows == []

    def test_a_quote_with_no_prices_is_still_a_row_of_nulls(self, world):
        world.adapter.quotes["2885"] = q(None, None, None, None, None)
        world.rec.on_alert(stock_alert())
        world.tick(NOW)
        row = world.rows[0]
        assert row["ltp"] is None and row["bid"] is None and row["ask"] is None

    def test_the_queue_holds_the_latest_two_hundred_alerts(self, world):
        for i in range(250):
            world.rec.on_alert(stock_alert(f"id{i:04d}"))
        assert world.tick(NOW) == 200
        ids = {r["alert_id"] for r in world.rows}
        assert "id0249" in ids and "id0050" in ids and "id0049" not in ids


class TestTheFollowUps:
    def run(self, world, *seconds):
        got = []
        for s in seconds:
            got.append(world.tick(NOW + dt.timedelta(seconds=s)))
        return got

    def test_follow_ups_come_at_60_120_and_300_seconds(self, world):
        assert FOLLOW_OFFSETS == (60, 120, 300)
        world.rec.on_alert(stock_alert())
        got = self.run(world, 0, 59, 60, 61, 119, 120, 121, 299, 300, 301, 3600)
        assert got == [1, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0]
        follow = [r for r in world.rows if r["kind"] == "FOLLOW"]
        assert [r["offset_sec"] for r in follow] == [60, 120, 300]
        assert all(r["alert_id"] == "aaaa11112222" and r["side"] == "BUY" for r in follow)

    def test_a_follow_up_is_stamped_when_it_was_taken_not_when_it_was_due(self, world):
        world.rec.on_alert(stock_alert())
        self.run(world, 0)
        world.tick(NOW + dt.timedelta(seconds=75))
        follow = [r for r in world.rows if r["kind"] == "FOLLOW"][0]
        assert follow["ts"] == (NOW + dt.timedelta(seconds=75)).astimezone(dt.timezone.utc)

    def test_a_late_tick_collects_every_follow_up_that_fell_due(self, world):
        world.rec.on_alert(stock_alert())
        world.tick(NOW)
        assert world.tick(NOW + dt.timedelta(seconds=400)) == 3
        assert sorted(r["offset_sec"] for r in world.rows if r["kind"] == "FOLLOW") == [
            60,
            120,
            300,
        ]

    def test_follow_ups_are_timed_from_the_alert_quote_not_from_the_alert(self, world):
        world.rec.on_alert(stock_alert())
        world.tick(NOW + dt.timedelta(seconds=30))  # the first tick after the alert
        assert world.tick(NOW + dt.timedelta(seconds=30 + 59)) == 0
        assert world.tick(NOW + dt.timedelta(seconds=30 + 60)) == 1

    def test_two_alerts_have_their_own_follow_ups(self, world):
        world.rec.on_alert(stock_alert("first"))
        world.tick(NOW)
        world.rec.on_alert(stock_alert("second"))
        world.tick(NOW + dt.timedelta(seconds=30))
        world.tick(NOW + dt.timedelta(seconds=61))
        world.tick(NOW + dt.timedelta(seconds=91))
        follow = {(r["alert_id"], r["offset_sec"]) for r in world.rows if r["kind"] == "FOLLOW"}
        assert follow == {("first", 60), ("second", 60)}

    def test_a_follow_up_whose_quote_is_missing_is_dropped_not_kept_waiting(self, world):
        world.rec.on_alert(stock_alert())
        world.tick(NOW)
        world.adapter.quotes.pop("2885")
        assert world.tick(NOW + dt.timedelta(seconds=60)) == 0
        world.adapter.quotes["2885"] = q(1000.0, 999.9, 1000.1)
        assert world.tick(NOW + dt.timedelta(seconds=61)) == 0  # the 60 s one is gone
        assert world.tick(NOW + dt.timedelta(seconds=120)) == 1

    def test_follow_ups_of_an_option_alert_quote_the_same_contract(self, world):
        world.rec.on_alert(option_alert())
        world.tick(NOW)
        world.tick(NOW + dt.timedelta(seconds=60))
        assert {r["security_id"] for r in world.rows} == {"70002"}

    def test_follow_ups_are_quoted_even_after_the_market_has_shut(self, world):
        world.open = False
        world.rec.on_alert(stock_alert())
        world.tick(NOW)
        assert world.tick(NOW + dt.timedelta(seconds=60)) == 1


class TestTheSamples:
    def test_nothing_is_sampled_while_the_market_is_shut(self, world):
        world.open = False
        for s in (0, 300, 600, 3000):
            assert world.tick(NOW + dt.timedelta(seconds=s)) == 0
        assert world.adapter.quote_calls == [] and world.adapter.chain_calls == []

    def test_an_open_market_samples_every_stock_and_the_two_options_of_each_index(self, world):
        world.open = True
        assert world.tick(NOW) == 3
        assert {r["kind"] for r in world.rows} == {"SAMPLE"}
        assert {r["security_id"] for r in world.rows} == {"2885", "70002", "70012"}
        for r in world.rows:
            assert r["alert_id"] is None and r["side"] is None and r["offset_sec"] is None

    def test_one_request_carries_every_sample(self, world):
        world.open = True
        world.tick(NOW)
        assert len(world.adapter.quote_calls) == 1
        assert {c.security_id for c in world.adapter.quote_calls[0]} == {"2885", "70002", "70012"}

    @pytest.mark.parametrize(
        ("seconds", "sampled"),
        [(1, False), (299, False), (300, True), (301, True), (900, True)],
    )
    def test_samples_are_taken_every_sample_every_seconds(self, world, seconds, sampled):
        world.open = True
        world.tick(NOW)
        before = len(world.rows)
        world.tick(NOW + dt.timedelta(seconds=seconds))
        assert (len(world.rows) > before) is sampled

    def test_the_interval_is_a_parameter(self, engine):
        w = World(engine, FakeAdapter(quotes={"2885": q(1.0, 0.9, 1.1)}), sample_every=60)
        w.open = True
        w.indices = []
        w.tick(NOW)
        assert w.tick(NOW + dt.timedelta(seconds=59)) == 0
        assert w.tick(NOW + dt.timedelta(seconds=60)) == 1

    def test_the_clock_of_the_last_sample_only_moves_when_one_is_taken(self, world):
        world.open = False
        world.tick(NOW)
        world.open = True
        assert world.tick(NOW + dt.timedelta(seconds=10)) == 3  # the first open tick samples

    def test_a_tick_that_samples_and_an_alert_share_one_request(self, world):
        world.open = True
        world.rec.on_alert(stock_alert())
        assert world.tick(NOW) == 4
        assert len(world.adapter.quote_calls) == 1
        assert {r["kind"] for r in world.rows} == {"ALERT", "SAMPLE"}

    def test_a_missing_quote_skips_that_instrument_only(self, world):
        world.open = True
        world.adapter.quotes.pop("70012")
        assert world.tick(NOW) == 2

    def test_the_watched_stocks_are_asked_again_every_tick(self, world):
        world.open = True
        world.stocks = [STOCK]
        world.tick(NOW)
        world.stocks = [STOCK, OTHER_STOCK]
        world.tick(NOW + dt.timedelta(seconds=300))
        sampled = [r["security_id"] for r in world.rows[3:]]
        assert "3045" in sampled and "2885" in sampled


class TestTheOptionsItSamples:
    def test_it_takes_the_call_and_put_nearest_the_target_delta_of_the_monthly_chain(self, world):
        world.open = True
        world.tick(NOW)
        assert world.adapter.expiry_calls == [(25, "IDX_I")]
        assert world.adapter.chain_calls == [("BANKNIFTY", "2026-11-24", 25, "IDX_I")]
        by_id = {r["security_id"]: r for r in world.rows}
        assert by_id["70002"]["label"] == "BANKNIFTY 45000 CE 2026-11-24"
        assert by_id["70012"]["label"] == "BANKNIFTY 45500 PE 2026-11-24"
        assert by_id["70002"]["segment"] == by_id["70012"]["segment"] == "NSE_FNO"
        assert by_id["70002"]["instrument_key"] == "BANKNIFTY_OPT"

    def test_rows_without_an_id_or_without_a_delta_are_never_picked(self, world):
        world.open = True
        world.tick(NOW)
        ids = {r["security_id"] for r in world.rows}
        assert "70099" not in ids and "None" not in ids

    def test_the_target_delta_is_a_parameter_and_the_sign_of_a_put_is_ignored(self, engine):
        w = World(engine, FakeAdapter(chains={"BANKNIFTY": chain_rows()}), target_delta=0.80)
        w.open = True
        w.stocks = []
        w.adapter.quotes.update({"70001": q(1, 1, 1), "70013": q(1, 1, 1)})
        w.tick(NOW)
        assert {r["security_id"] for r in w.rows} == {"70001", "70013"}

    def test_a_chain_with_only_puts_gives_only_a_put(self, engine):
        only_puts = [r for r in chain_rows() if r.opt_type == "PE"]
        w = World(engine, FakeAdapter(chains={"BANKNIFTY": only_puts}))
        w.open, w.stocks = True, []
        w.adapter.quotes["70012"] = q(380.0, 379.0, 381.0)
        w.tick(NOW)
        assert {r["security_id"] for r in w.rows} == {"70012"}

    def test_the_contract_is_the_monthly_and_rolls_inside_the_window(self, world):
        world.open, world.stocks = True, []
        world.tick(dt.datetime(2026, 11, 22, 10, 0, tzinfo=IST))
        assert world.adapter.chain_calls[0][1] == "2026-12-29"

    def test_roll_days_is_a_parameter(self, engine):
        w = World(engine, FakeAdapter(chains={"BANKNIFTY": chain_rows()}), roll_days=1)
        w.open, w.stocks = True, []
        w.tick(dt.datetime(2026, 11, 22, 10, 0, tzinfo=IST))
        assert w.adapter.chain_calls[0][1] == "2026-11-24"

    def test_the_chain_is_read_again_only_after_chain_every_seconds(self, world):
        world.open, world.stocks = True, []
        for s in range(0, 1800, 300):  # six sampling ticks inside the half hour
            world.tick(NOW + dt.timedelta(seconds=s))
        assert len(world.adapter.chain_calls) == 1  # sampled each time, read once
        assert len(world.adapter.quote_calls) == 6
        world.tick(NOW + dt.timedelta(seconds=1800))  # exactly chain_every later: read again
        assert len(world.adapter.chain_calls) == 2

    def test_the_options_found_stay_sampled_between_chain_reads(self, world):
        world.open, world.stocks = True, []
        world.tick(NOW)
        world.tick(NOW + dt.timedelta(seconds=300))
        assert len([r for r in world.rows if r["security_id"] == "70002"]) == 2

    def test_no_contract_far_enough_means_no_option_samples_and_no_chain_request(self, engine):
        w = World(engine, FakeAdapter(expiries=["2026-11-07"], chains={"BANKNIFTY": chain_rows()}))
        w.open, w.stocks = True, []
        assert w.tick(NOW) == 0 and w.adapter.chain_calls == []

    def test_one_index_failing_never_stops_the_others(self, engine):
        w = World(
            engine,
            FakeAdapter(chains={"BANKNIFTY": RuntimeError("DH-904"), "NIFTY": chain_rows()}),
        )
        w.indices = [
            ("BANKNIFTY", "25", "NSE", "BANKNIFTY_OPT"),
            ("NIFTY", "13", "NSE", "NIFTY_OPT"),
        ]
        w.open, w.stocks = True, []
        w.adapter.quotes.update({"70002": q(345.0, 344.0, 346.0), "70012": q(380.0, 379.0, 381.0)})
        assert w.tick(NOW) == 2
        assert {r["instrument_key"] for r in w.rows} == {"NIFTY_OPT"}

    def test_a_chain_that_fails_everywhere_keeps_what_was_found_before(self, engine):
        adapter = FakeAdapter(chains={"BANKNIFTY": chain_rows()})
        w = World(engine, adapter, chain_every=60)
        w.open, w.stocks = True, []
        adapter.quotes.update({"70002": q(345.0, 344.0, 346.0), "70012": q(380.0, 379.0, 381.0)})
        w.tick(NOW)
        adapter.chains["BANKNIFTY"] = RuntimeError("down")
        assert w.tick(NOW + dt.timedelta(seconds=300)) == 2  # the old ids still sampled

    def test_an_index_with_a_bse_listing_names_the_bse_segment(self, engine):
        w = World(engine, FakeAdapter(chains={"SENSEX": chain_rows()}))
        w.indices = [("SENSEX", "51", "BSE", "SENSEX_OPT")]
        w.open, w.stocks = True, []
        w.adapter.quotes["70002"] = q(1, 1, 1)
        w.tick(NOW)
        assert w.rows[0]["segment"] == "BSE_FNO"

    def test_an_option_the_chain_does_not_know_stays_unsampled(self, world):
        world.open, world.stocks = True, []
        world.indices = [("BANKNIFTY", "25", "NSE", "BANKNIFTY_OPT")]
        world.adapter.chains["BANKNIFTY"] = []
        assert world.tick(NOW) == 0


class TestFailuresDoNotCorruptTheRecorder:
    def test_a_broker_error_comes_out_for_the_loop_to_count(self, world):
        world.adapter.fail_quote = True
        world.rec.on_alert(stock_alert())
        with pytest.raises(RuntimeError, match="DH-904"):
            world.tick(NOW)

    def test_after_an_error_the_next_alert_is_quoted_normally(self, world):
        world.adapter.fail_quote = True
        world.rec.on_alert(stock_alert("lost"))
        with pytest.raises(RuntimeError):
            world.tick(NOW)
        world.adapter.fail_quote = False
        world.rec.on_alert(stock_alert("kept"))
        world.tick(NOW + dt.timedelta(seconds=15))
        assert "kept" in [r["alert_id"] for r in world.rows if r["kind"] == "ALERT"]

    def test_an_alert_whose_quote_failed_is_quoted_on_the_next_tick(self, world):
        """Tester observation (fixed): a broker error must not lose that tick's ALERT."""
        world.adapter.fail_quote = True
        world.rec.on_alert(stock_alert("retried"))
        with pytest.raises(RuntimeError):
            world.tick(NOW)
        world.adapter.fail_quote = False
        world.tick(NOW + dt.timedelta(seconds=10))
        alerts = [r for r in world.rows if r["kind"] == "ALERT"]
        assert [r["alert_id"] for r in alerts] == ["retried"]
        # its follow-ups count from the quote that was taken, once each, never twice
        for off in (60, 120, 300):
            world.tick(NOW + dt.timedelta(seconds=10 + off))
        follows = [r["offset_sec"] for r in world.rows if r["kind"] == "FOLLOW"]
        assert follows == [60, 120, 300]

    def test_follow_ups_that_were_due_when_the_quote_failed_are_taken_next_tick(self, world):
        world.rec.on_alert(stock_alert("f1"))
        world.tick(NOW)
        world.adapter.fail_quote = True
        with pytest.raises(RuntimeError):
            world.tick(NOW + dt.timedelta(seconds=61))
        world.adapter.fail_quote = False
        world.tick(NOW + dt.timedelta(seconds=71))
        assert [r["offset_sec"] for r in world.rows if r["kind"] == "FOLLOW"] == [60]

    def test_start_runs_one_thread_and_stop_ends_it(self, engine):
        w = World(engine, None)  # no adapter: every tick is a no-op
        w.rec.start()
        first = w.rec._thread
        w.rec.start()
        assert w.rec._thread is first and first is not None and first.is_alive()
        w.rec.stop()
        first.join(timeout=5)
        assert not first.is_alive()
        assert [t for t in threading.enumerate() if t.name == "fill-costs"] == []

    def test_a_recorder_asks_the_broker_for_quotes_expiries_and_chains_and_nothing_else(
        self, world
    ):
        allowed = {"quote", "expiry_list_raw", "option_chain"}
        touched: list[str] = []

        class ReadOnly:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                touched.append(name)
                if name not in allowed:
                    raise AssertionError(f"the recorder touched adapter.{name}")
                return getattr(self._inner, name)

        world.adapter = ReadOnly(world.adapter)
        world.open = True
        world.rec.on_alert(stock_alert())
        world.rec.on_alert(option_alert())
        world.tick(NOW)
        world.tick(NOW + dt.timedelta(seconds=400))
        assert set(touched) == allowed


class TestAlertsFromOtherThreads:
    def test_alerts_raised_while_it_ticks_are_each_quoted_exactly_once(self, world):
        errors: list[BaseException] = []
        stop = threading.Event()

        def raise_alerts(worker: int) -> None:
            try:
                for i in range(50):
                    world.rec.on_alert(stock_alert(f"w{worker}-{i:03d}"))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=raise_alerts, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        ticks = 0
        while any(t.is_alive() for t in threads) or ticks < 3:
            world.tick(NOW + dt.timedelta(seconds=ticks))
            ticks += 1
        stop.set()
        for t in threads:
            t.join(timeout=10)
        world.tick(NOW + dt.timedelta(seconds=ticks))
        assert not errors
        alert_ids = [r["alert_id"] for r in world.rows if r["kind"] == "ALERT"]
        assert len(alert_ids) == 200 and len(set(alert_ids)) == 200


class TestTheTable:
    def test_the_recorder_creates_its_table_on_an_older_database(self, tmp_path):
        eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
        assert not inspect(eng).has_table("fill_quotes")
        World(eng, None)
        assert inspect(eng).has_table("fill_quotes")
        assert FillCostRecorder(eng, lambda: None, lambda: True, lambda n: False,
                                lambda: [], lambda: []) is not None  # a second start: no error  # fmt: skip
        eng.dispose()

    def test_a_fresh_database_has_it_with_its_index_and_columns(self, engine):
        insp = inspect(engine)
        assert insp.has_table("fill_quotes")
        cols = [c["name"] for c in insp.get_columns("fill_quotes")]
        assert cols == [
            "id", "ts", "kind", "instrument_key", "security_id", "segment", "label", "alert_id",
            "side", "offset_sec", "ltp", "bid", "ask", "bid_qty", "ask_qty",
        ]  # fmt: skip
        assert any(i["column_names"] == ["ts"] for i in insp.get_indexes("fill_quotes"))

    def test_the_table_is_part_of_the_shared_metadata_and_the_data_schema_module(self):
        from onelot.data import schema

        assert "fill_quotes" in METADATA.tables and schema.fill_quotes is fill_quotes

    def test_a_row_round_trips_with_its_timestamp_in_utc(self, engine):
        with engine.begin() as conn:
            conn.execute(insert(fill_quotes), [{"ts": NOW, "kind": "SAMPLE",
                                                "instrument_key": "X", "security_id": "1"}])  # fmt: skip
        (row,) = rows_of(engine)
        assert row["ts"] == NOW and row["ts"].utcoffset() is not None


# ══ report ════════════════════════════════════════════════════════════════════════════════


TODAY = NOW.date()


def at(h: int, m: int, s: int = 0, day: dt.date = TODAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST)


def sample(engine, ts, key, sid, bid, ask, *, segment="NSE_EQ", label=None, kind="SAMPLE", **kw):
    row = {
        "ts": ts, "kind": kind, "instrument_key": key, "security_id": sid, "segment": segment,
        "label": label or key, "bid": bid, "ask": ask, "ltp": (bid or 0) / 2 + (ask or 0) / 2 or None,
        "bid_qty": 10, "ask_qty": 10,
    }  # fmt: skip
    row.update(kw)
    with engine.begin() as conn:
        conn.execute(insert(fill_quotes), [row])


REPORT_NOW = at(16, 0)


class TestReportSpreads:
    def test_median_and_p90_per_instrument(self, engine):
        # half-spreads 0.05 / 0.08 / 0.10 / 0.12 / 0.30 % around a mid of 100
        for i, hs in enumerate([0.05, 0.08, 0.10, 0.12, 0.30]):
            sample(engine, at(10, i), "RELIANCE", "2885", 100 - hs, 100 + hs)
        r = report(engine, 30, REPORT_NOW)
        (row,) = r["by_instrument"]
        assert row["instrument"] == "RELIANCE" and row["kind"] == "stock" and row["samples"] == 5
        assert row["half_spread_median_pct"] == pytest.approx(0.10)
        assert row["half_spread_p90_pct"] == pytest.approx(0.30)
        assert r["samples"] == 5

    def test_the_buckets_are_the_four_parts_of_the_session(self):
        assert BUCKETS == (
            ("09:15", "10:00"),
            ("10:00", "11:30"),
            ("11:30", "13:00"),
            ("13:00", "15:30"),
        )

    @pytest.mark.parametrize(
        ("h", "m", "bucket"),
        [
            (9, 15, "09:15-10:00"), (9, 59, "09:15-10:00"),
            (10, 0, "10:00-11:30"), (11, 29, "10:00-11:30"),
            (11, 30, "11:30-13:00"), (12, 59, "11:30-13:00"),
            (13, 0, "13:00-15:30"), (15, 29, "13:00-15:30"),
        ],
    )  # fmt: skip
    def test_a_sample_falls_in_the_bucket_of_its_ist_time(self, engine, h, m, bucket):
        sample(engine, at(h, m), "RELIANCE", "2885", 99.9, 100.1)
        (row,) = report(engine, 30, REPORT_NOW)["by_instrument"]
        assert list(row["by_time"]) == [bucket]
        assert row["by_time"][bucket] == pytest.approx(0.1)

    def test_each_bucket_has_its_own_median_and_they_are_listed_in_order(self, engine):
        for h, m, hs in [(13, 5, 0.30), (9, 20, 0.04), (11, 40, 0.20), (10, 30, 0.10)]:
            sample(engine, at(h, m), "RELIANCE", "2885", 100 - hs, 100 + hs)
        (row,) = report(engine, 30, REPORT_NOW)["by_instrument"]
        assert list(row["by_time"]) == [
            "09:15-10:00", "10:00-11:30", "11:30-13:00", "13:00-15:30",
        ]  # fmt: skip
        assert list(row["by_time"].values()) == pytest.approx([0.04, 0.10, 0.20, 0.30])

    def test_a_sample_before_the_open_or_from_the_close_on_is_in_other(self, engine):
        sample(engine, at(9, 10), "RELIANCE", "2885", 99.9, 100.1)
        sample(engine, at(15, 30), "RELIANCE", "2885", 99.9, 100.1)
        (row,) = report(engine, 30, REPORT_NOW)["by_instrument"]
        assert list(row["by_time"]) == ["other"]

    def test_the_buckets_are_read_in_ist_whatever_the_database_stored(self, engine):
        sample(engine, dt.datetime(2026, 11, 6, 4, 30, tzinfo=dt.timezone.utc), "RELIANCE",
               "2885", 99.9, 100.1)  # 10:00 IST  # fmt: skip
        (row,) = report(engine, 30, REPORT_NOW)["by_instrument"]
        assert list(row["by_time"]) == ["10:00-11:30"]

    def test_an_unusable_quote_is_counted_as_a_sample_but_not_in_the_spread(self, engine):
        sample(engine, at(10, 0), "RELIANCE", "2885", 99.9, 100.1)
        sample(engine, at(10, 5), "RELIANCE", "2885", 100.2, 100.0)  # crossed
        sample(engine, at(10, 10), "RELIANCE", "2885", None, 100.0)  # one-sided
        r = report(engine, 30, REPORT_NOW)
        assert r["samples"] == 3 and r["by_instrument"][0]["samples"] == 1

    def test_an_instrument_with_only_unusable_quotes_does_not_appear(self, engine):
        sample(engine, at(10, 0), "RELIANCE", "2885", 100.2, 100.0)
        assert report(engine, 30, REPORT_NOW)["by_instrument"] == []

    def test_an_option_is_named_by_its_index_and_side_and_marked_an_option(self, engine):
        sample(engine, at(10, 0), "BANKNIFTY_OPT", "70002", 344.0, 346.0, segment="NSE_FNO",
               label="BANKNIFTY 45000 CE 2026-11-24")  # fmt: skip
        sample(engine, at(10, 0), "BANKNIFTY_OPT", "70012", 379.0, 381.0, segment="NSE_FNO",
               label="BANKNIFTY 45500 PE 2026-11-24")  # fmt: skip
        names = {
            x["instrument"]: x["kind"] for x in report(engine, 30, REPORT_NOW)["by_instrument"]
        }
        assert names == {"BANKNIFTY_OPT CE": "option", "BANKNIFTY_OPT PE": "option"}

    def test_the_instruments_are_listed_in_name_order(self, engine):
        for key in ("TCS", "RELIANCE", "SBIN"):
            sample(engine, at(10, 0), key, key, 99.9, 100.1)
        assert [x["instrument"] for x in report(engine, 30, REPORT_NOW)["by_instrument"]] == [
            "RELIANCE", "SBIN", "TCS",
        ]  # fmt: skip

    def test_the_measured_figures_are_the_median_of_the_per_instrument_medians(self, engine):
        for key, hs in (("RELIANCE", 0.05), ("SBIN", 0.15), ("TCS", 0.10)):
            sample(engine, at(10, 0), key, key, 100 - hs, 100 + hs)
        sample(engine, at(10, 0), "BANKNIFTY_OPT", "70002", 99.0, 101.0, segment="NSE_FNO",
               label="BANKNIFTY 45000 CE 2026-11-24")  # fmt: skip
        m = report(engine, 30, REPORT_NOW)["measured"]
        assert m["stock_half_spread_median_pct"] == pytest.approx(0.10)
        assert m["option_half_spread_median_pct"] == pytest.approx(1.0)

    def test_an_empty_database_still_answers_in_full_shape(self, engine):
        r = report(engine, 7, REPORT_NOW)
        assert r["days"] == 7 and r["samples"] == 0
        assert r["by_instrument"] == [] and r["alerts"] == [] and r["owner_fills"] == []
        assert r["measured"] == {
            "stock_half_spread_median_pct": None, "option_half_spread_median_pct": None,
        }  # fmt: skip
        assert "never an order" in r["note"]
        json.dumps(r)

    def test_the_window_keeps_only_the_last_days(self, engine):
        sample(engine, REPORT_NOW - dt.timedelta(days=2, hours=1), "RELIANCE", "2885", 99.0, 101.0)
        sample(engine, REPORT_NOW - dt.timedelta(days=1), "RELIANCE", "2885", 99.9, 100.1)
        assert report(engine, 2, REPORT_NOW)["samples"] == 1
        assert report(engine, 3, REPORT_NOW)["samples"] == 2

    def test_what_it_states_the_lab_assumes_is_what_the_lab_assumes(self):
        cfg = AppConfig.load()
        a = report.__globals__["LAB_STOCK_SLIPPAGE_PCT"]
        engine = create_engine("sqlite://")
        METADATA.create_all(engine)
        stated = report(engine, 1, REPORT_NOW)["lab_assumptions"]
        assert stated["stock_slippage_pct_per_side"] == a == BacktestCfg().slippage_pct
        assert stated["stock_slippage_pct_per_side"] == cfg.backtest.slippage_pct
        assert stated["option_half_spread_pct_per_side"] == pytest.approx(
            cfg.backtest.option_spread_pct * 100
        )
        assert stated["option_slippage_ticks_per_side"] == cfg.backtest.option_slippage_ticks
        engine.dispose()


def store_alert(engine, alert_id, created, *, entry_price=None, option=None):
    payload = {"id": alert_id, "economics": {"entry_price": entry_price, "option": option or {}}}
    with engine.begin() as conn:
        conn.execute(
            desk_signals.insert().values(
                id=alert_id, created_at=created, instrument_key="X", series_key="X", kind="STOCK",
                side="BUY", strategy="s", timeframe="5m", status="PENDING", payload=payload,
            )
        )  # fmt: skip


def quote_row(engine, kind, ts, *, alert_id, side, offset=None, bid=None, ask=None, ltp=None,
              key="RELIANCE", sid="2885", label=None):  # fmt: skip
    sample(engine, ts, key, sid, bid, ask, kind=kind, alert_id=alert_id, side=side,
           offset_sec=offset, ltp=ltp, label=label or key)  # fmt: skip


class TestReportAlerts:
    def test_a_buy_is_judged_at_the_ask_against_the_alerts_own_price(self, engine):
        store_alert(engine, "a1", at(10, 0), entry_price=1000.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="a1", side="BUY", bid=1000.5, ask=1001.5)
        r = report(engine, 30, REPORT_NOW)
        (a,) = r["alerts"]
        assert a["alert_id"] == "a1" and a["side"] == "BUY" and a["instrument"] == "RELIANCE"
        assert a["fill_vs_alert_pct"] == pytest.approx((1001.5 - 1000.0) / 1000.0 * 100)
        assert a["half_spread_pct"] == pytest.approx(0.5 / 1001.0 * 100)
        assert a["at"] == at(10, 0, 5).isoformat()

    def test_a_sell_is_judged_at_the_bid_and_lower_is_worse(self, engine):
        store_alert(engine, "a2", at(10, 0), entry_price=1000.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="a2", side="SELL", bid=999.0, ask=1000.5)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert a["fill_vs_alert_pct"] == pytest.approx(0.1)  # sold at 999 for an alert at 1000

    def test_the_drift_after_the_alert_is_signed_worse_positive(self, engine):
        store_alert(engine, "a3", at(10, 0), entry_price=345.0)
        base = {"alert_id": "a3", "side": "BUY_CE", "key": "BANKNIFTY_OPT", "sid": "70002"}
        quote_row(engine, "ALERT", at(10, 0, 5), bid=344.0, ask=346.0, **base)
        quote_row(engine, "FOLLOW", at(10, 1, 5), offset=60, bid=345.0, ask=347.0, **base)
        quote_row(engine, "FOLLOW", at(10, 2, 5), offset=120, bid=343.0, ask=345.0, **base)
        quote_row(engine, "FOLLOW", at(10, 5, 5), offset=300, bid=349.0, ask=351.0, **base)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert a["after_60s_pct"] == pytest.approx((347 - 346) / 346 * 100)  # dearer: worse
        assert a["after_120s_pct"] == pytest.approx((345 - 346) / 346 * 100)  # cheaper: better
        assert a["after_300s_pct"] == pytest.approx((351 - 346) / 346 * 100)

    def test_for_a_sell_a_falling_price_is_the_worse_drift(self, engine):
        store_alert(engine, "a4", at(10, 0), entry_price=100.0)
        base = {"alert_id": "a4", "side": "SELL"}
        quote_row(engine, "ALERT", at(10, 0, 5), bid=100.0, ask=100.2, **base)
        quote_row(engine, "FOLLOW", at(10, 1, 5), offset=60, bid=99.0, ask=99.2, **base)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert a["after_60s_pct"] == pytest.approx(1.0)

    def test_only_the_follow_ups_that_exist_are_reported(self, engine):
        store_alert(engine, "a5", at(10, 0), entry_price=100.0)
        base = {"alert_id": "a5", "side": "BUY"}
        quote_row(engine, "ALERT", at(10, 0, 5), bid=99.9, ask=100.1, **base)
        quote_row(engine, "FOLLOW", at(10, 2, 5), offset=120, bid=99.9, ask=100.1, **base)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert "after_120s_pct" in a and "after_60s_pct" not in a and "after_300s_pct" not in a

    def test_with_only_a_last_price_it_is_the_last_price_that_is_judged(self, engine):
        store_alert(engine, "a6", at(10, 0), entry_price=100.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="a6", side="BUY", ltp=100.5)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert a["fill_vs_alert_pct"] == pytest.approx(0.5) and a["half_spread_pct"] is None

    def test_an_alert_whose_quote_has_nothing_to_judge_is_left_out(self, engine):
        store_alert(engine, "a7", at(10, 0), entry_price=100.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="a7", side="BUY")
        assert report(engine, 30, REPORT_NOW)["alerts"] == []

    def test_follow_ups_without_their_alert_quote_are_left_out(self, engine):
        store_alert(engine, "a8", at(10, 0), entry_price=100.0)
        quote_row(engine, "FOLLOW", at(10, 1, 5), alert_id="a8", side="BUY", offset=60, bid=99.0,
                  ask=101.0)  # fmt: skip
        assert report(engine, 30, REPORT_NOW)["alerts"] == []

    def test_an_alert_quote_without_a_side_is_left_out(self, engine):
        store_alert(engine, "a9", at(10, 0), entry_price=100.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="a9", side=None, bid=99.0, ask=101.0)
        assert report(engine, 30, REPORT_NOW)["alerts"] == []

    def test_an_alert_the_database_no_longer_holds_is_reported_without_a_fill_figure(self, engine):
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="old1", side="BUY", bid=99.0, ask=101.0)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert a["fill_vs_alert_pct"] is None and a["half_spread_pct"] == pytest.approx(1.0)

    @pytest.mark.parametrize("price", [None, 0, -5.0, "n/a"])
    def test_an_alert_with_no_usable_price_has_no_fill_figure(self, engine, price):
        store_alert(engine, "b1", at(10, 0), entry_price=price)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="b1", side="BUY", bid=99.0, ask=101.0)
        (a,) = report(engine, 30, REPORT_NOW)["alerts"]
        assert a["fill_vs_alert_pct"] is None

    def test_the_instrument_shown_is_the_label_else_the_key(self, engine):
        store_alert(engine, "b2", at(10, 0), entry_price=100.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="b2", side="BUY", bid=99.0, ask=101.0,
                  label="BANKNIFTY 45000 CE 2026-11-24")  # fmt: skip
        assert report(engine, 30, REPORT_NOW)["alerts"][0]["instrument"] == (
            "BANKNIFTY 45000 CE 2026-11-24"
        )

    def test_alerts_outside_the_window_are_not_reported(self, engine):
        old = REPORT_NOW - dt.timedelta(days=10)
        store_alert(engine, "c1", old, entry_price=100.0)
        quote_row(engine, "ALERT", old, alert_id="c1", side="BUY", bid=99.0, ask=101.0)
        assert report(engine, 5, REPORT_NOW)["alerts"] == []
        assert len(report(engine, 30, REPORT_NOW)["alerts"]) == 1

    def test_the_report_is_plain_json(self, engine):
        store_alert(engine, "d1", at(10, 0), entry_price=100.0)
        quote_row(engine, "ALERT", at(10, 0, 5), alert_id="d1", side="BUY", bid=99.0, ask=101.0)
        json.dumps(report(engine, 30, REPORT_NOW))


def store_position(engine, pid, opened, **payload):
    with engine.begin() as conn:
        conn.execute(
            desk_positions.insert().values(
                id=pid, signal_id=pid, instrument_key="RELIANCE", series_key="RELIANCE",
                kind="STOCK", side=payload.get("side", "BUY"), opened_at=opened, status="CLOSED",
                payload={"id": pid, "instrument_key": "RELIANCE", **payload},
            )
        )  # fmt: skip


class TestReportOwnerFills:
    def test_a_buy_paid_dearer_than_the_alert_is_a_positive_entry_slippage(self, engine):
        store_position(engine, "p1", at(10, 0), side="BUY", user_entry_price=100.5,
                       alert_entry_price=100.0)  # fmt: skip
        (f,) = report(engine, 30, REPORT_NOW)["owner_fills"]
        assert (f["position"], f["instrument"], f["leg"]) == ("p1", "RELIANCE", "entry")
        assert (f["alert_price"], f["your_price"]) == (100.0, 100.5)
        assert f["slippage_pct"] == pytest.approx(0.5)

    def test_the_exit_of_a_buy_is_a_sell_so_lower_is_worse(self, engine):
        store_position(engine, "p2", at(10, 0), side="BUY", user_exit_price=99.0,
                       alert_exit_price=100.0)  # fmt: skip
        (f,) = report(engine, 30, REPORT_NOW)["owner_fills"]
        assert f["leg"] == "exit" and f["slippage_pct"] == pytest.approx(1.0)

    def test_a_short_sold_lower_is_worse_and_covered_higher_is_worse(self, engine):
        store_position(
            engine, "p3", at(10, 0), side="SELL", user_entry_price=99.5, alert_entry_price=100.0,
            user_exit_price=101.0, alert_exit_price=100.0,
        )  # fmt: skip
        legs = {f["leg"]: f["slippage_pct"] for f in report(engine, 30, REPORT_NOW)["owner_fills"]}
        assert legs["entry"] == pytest.approx(0.5) and legs["exit"] == pytest.approx(1.0)

    def test_better_than_the_alert_is_negative(self, engine):
        store_position(engine, "p4", at(10, 0), side="BUY", user_entry_price=99.5,
                       alert_entry_price=100.0)  # fmt: skip
        assert report(engine, 30, REPORT_NOW)["owner_fills"][0]["slippage_pct"] == pytest.approx(
            -0.5
        )

    def test_the_alerts_entry_price_falls_back_to_the_positions_entry_price(self, engine):
        store_position(engine, "p5", at(10, 0), side="BUY", user_entry_price=100.5,
                       entry_price=100.0)  # fmt: skip
        assert report(engine, 30, REPORT_NOW)["owner_fills"][0]["alert_price"] == 100.0

    @pytest.mark.parametrize(
        "prices",
        [{"user_entry_price": 100.5}, {"alert_entry_price": 100.0},
         {"user_entry_price": None, "alert_entry_price": 100.0},
         {"user_entry_price": 0, "alert_entry_price": 100.0},
         {"user_entry_price": 100.5, "alert_entry_price": -1.0}, {}],
    )  # fmt: skip
    def test_a_fill_without_both_prices_is_not_reported(self, engine, prices):
        store_position(engine, "p6", at(10, 0), side="BUY", **prices)
        assert report(engine, 30, REPORT_NOW)["owner_fills"] == []

    def test_an_option_buy_side_reads_as_a_buy(self, engine):
        store_position(engine, "p7", at(10, 0), side="BUY_CE", user_entry_price=348.0,
                       alert_entry_price=345.0)  # fmt: skip
        assert report(engine, 30, REPORT_NOW)["owner_fills"][0]["slippage_pct"] == pytest.approx(
            3 / 345 * 100
        )

    def test_a_position_without_a_side_is_taken_as_a_buy(self, engine):
        store_position(engine, "p8", at(10, 0), user_entry_price=100.5, alert_entry_price=100.0)
        with engine.begin() as conn:
            conn.execute(desk_positions.update().values(side=None))
        # the payload (not the column) decides, and a payload without "side" reads as BUY
        assert report(engine, 30, REPORT_NOW)["owner_fills"][0]["slippage_pct"] == pytest.approx(
            0.5
        )

    def test_only_positions_opened_inside_the_window_count(self, engine):
        store_position(engine, "p9", REPORT_NOW - dt.timedelta(days=40), side="BUY",
                       user_entry_price=100.5, alert_entry_price=100.0)  # fmt: skip
        assert report(engine, 30, REPORT_NOW)["owner_fills"] == []
        assert len(report(engine, 60, REPORT_NOW)["owner_fills"]) == 1


# ══ the API ═══════════════════════════════════════════════════════════════════════════════


def client_for(desk: Any) -> TestClient:
    stub = MagicMock()
    stub.desk = desk
    return TestClient(create_app(stub, "test-token"), raise_server_exceptions=False)


class TestFillCostsEndpoint:
    def test_it_serves_the_report_and_the_charges(self, engine):
        sample(engine, at(10, 0), "RELIANCE", "2885", 99.9, 100.1)
        desk = SimpleNamespace(engine=engine, fill_charges=lambda: {"fills": 3, "by_product": []})
        body = client_for(desk).get("/api/desk/fill-costs").json()
        assert body["days"] == 30 and "lab_assumptions" in body and "by_instrument" in body
        assert body["charges"] == {"fills": 3, "by_product": []}

    def test_days_reaches_the_report(self, engine):
        sample(engine, dt.datetime.now(IST) - dt.timedelta(days=3), "RELIANCE", "2885", 99.9, 100.1)
        desk = SimpleNamespace(engine=engine, fill_charges=lambda: {})
        c = client_for(desk)
        assert c.get("/api/desk/fill-costs?days=2").json()["samples"] == 0
        assert c.get("/api/desk/fill-costs?days=5").json()["samples"] == 1
        assert c.get("/api/desk/fill-costs?days=5").json()["days"] == 5

    @pytest.mark.parametrize("days", ["0", "-1", "366", "abc", "1.5", ""])
    def test_a_days_outside_one_to_365_is_refused(self, engine, days):
        desk = SimpleNamespace(engine=engine, fill_charges=lambda: {})
        assert client_for(desk).get(f"/api/desk/fill-costs?days={days}").status_code == 422

    @pytest.mark.parametrize("days", [1, 365])
    def test_the_edges_are_allowed(self, engine, days):
        desk = SimpleNamespace(engine=engine, fill_charges=lambda: {})
        assert client_for(desk).get(f"/api/desk/fill-costs?days={days}").status_code == 200

    def test_a_trade_book_that_fails_leaves_the_quotes_and_names_the_error(self, engine):
        def boom():
            raise RuntimeError("DH-901 token expired " + "x" * 400)

        body = (
            client_for(SimpleNamespace(engine=engine, fill_charges=boom))
            .get("/api/desk/fill-costs")
            .json()
        )
        assert body["charges"]["error"].startswith("RuntimeError: DH-901 token expired")
        assert len(body["charges"]["error"]) <= 160 + len("RuntimeError: ")
        assert body["days"] == 30  # the quote report is intact

    def test_without_a_broker_the_charges_are_empty(self, engine):
        body = (
            client_for(SimpleNamespace(engine=engine, fill_charges=lambda: {}))
            .get("/api/desk/fill-costs")
            .json()
        )
        assert body["charges"] == {}

    def test_without_a_desk_it_says_so(self):
        r = client_for(None).get("/api/desk/fill-costs")
        assert r.status_code == 503 and "Live Desk" in r.json()["detail"]

    def test_it_is_a_read_not_a_write(self, engine):
        c = client_for(SimpleNamespace(engine=engine, fill_charges=lambda: {}))
        assert c.post("/api/desk/fill-costs").status_code == 405
        assert c.delete("/api/desk/fill-costs").status_code == 405


# ══ charges ═══════════════════════════════════════════════════════════════════════════════


class TestCostProduct:
    @pytest.mark.parametrize(
        ("row", "product"),
        [
            ({"exchangeSegment": "NSE_EQ", "productType": "INTRADAY"}, "equity_intraday"),
            ({"exchangeSegment": "BSE_EQ", "productType": "INTRADAY"}, "equity_intraday"),
            ({"exchangeSegment": "NSE_EQ", "productType": "CNC"}, "equity_delivery"),
            ({"exchangeSegment": "NSE_EQ", "productType": "MTF"}, "equity_delivery"),
            ({"exchangeSegment": "NSE_EQ"}, "equity_delivery"),
            ({"exchangeSegment": "NSE_FNO", "instrument": "OPTIDX"}, "index_options"),
            ({"exchangeSegment": "BSE_FNO", "instrument": "OPTIDX"}, "index_options"),
            ({"exchangeSegment": "NSE_FNO", "instrument": "OPTSTK"}, "stock_options"),
            ({"exchangeSegment": "NSE_FNO", "instrument": "FUTIDX"}, "index_futures"),
            ({"exchangeSegment": "NSE_FNO", "instrument": "FUTSTK"}, "stock_futures"),
            ({"exchangeSegment": "NSE_CURRENCY", "drvOptionType": "CALL"}, "currency_options"),
            ({"exchangeSegment": "NSE_CURRENCY", "drvOptionType": "PUT"}, "currency_options"),
            ({"exchangeSegment": "NSE_CURRENCY"}, "currency_futures"),
            ({"exchangeSegment": "NSE_CURRENCY", "drvOptionType": None}, "currency_futures"),
            ({"exchangeSegment": "NSE_CURRENCY", "drvOptionType": ""}, "currency_futures"),
        ],
    )
    def test_a_trade_book_row_is_charged_as_its_product(self, row, product):
        assert cost_product(row) == product

    @pytest.mark.parametrize(
        "row",
        [
            {}, {"exchangeSegment": None}, {"exchangeSegment": ""}, {"exchangeSegment": "IDX_I"},
            {"exchangeSegment": "MCX_COMM", "instrument": "FUTCOM"},
            {"exchangeSegment": "NSE_FNO"}, {"exchangeSegment": "NSE_FNO", "instrument": "EQUITY"},
            {"exchangeSegment": "NSE_FNO", "instrument": "optidx"}, {"exchangeSegment": "nse_eq"},
        ],
    )  # fmt: skip
    def test_what_the_cost_table_does_not_model_is_none(self, row):
        assert cost_product(row) is None

    def test_every_product_it_can_name_exists_in_the_cost_table(self):
        table = CostConfig.load(ROOT / "config" / "costs.yaml")
        named = {
            cost_product(r)
            for r in (
                {"exchangeSegment": "NSE_EQ", "productType": "INTRADAY"},
                {"exchangeSegment": "NSE_EQ", "productType": "CNC"},
                {"exchangeSegment": "NSE_FNO", "instrument": "OPTIDX"},
                {"exchangeSegment": "NSE_FNO", "instrument": "OPTSTK"},
                {"exchangeSegment": "NSE_FNO", "instrument": "FUTIDX"},
                {"exchangeSegment": "NSE_FNO", "instrument": "FUTSTK"},
                {"exchangeSegment": "NSE_CURRENCY", "drvOptionType": "CALL"},
                {"exchangeSegment": "NSE_CURRENCY"},
            )
        }
        assert named <= set(table.products), named - set(table.products)  # no silent fallback


class ScriptedCosts:
    """A cost model of 0.1 % of turnover, plus 1.0 on a buy and 2.0 on a sell."""

    def __init__(self):
        self.calls: list[tuple[str, float, int, str]] = []

    def __call__(self, product):
        outer = self

        class Model:
            def leg(self, price, qty, side):
                outer.calls.append((product, price, qty, side))
                return SimpleNamespace(total=price * qty * 0.001 + (1.0 if side == "buy" else 2.0))

        return Model()


def trade(side="BUY", qty=30, price=100.0, seg="NSE_FNO", instrument="OPTIDX", **charges):
    row = {"exchangeSegment": seg, "instrument": instrument, "transactionType": side,
           "tradedQuantity": qty, "tradedPrice": price}  # fmt: skip
    row.update(charges)
    return row


class TestChargesCheck:
    def test_one_group_per_product_and_side_with_its_totals(self):
        rows = [
            trade(
                "BUY", 30, 100.0, brokerageCharges=20.0, stt=0.0, exchangeTransactionCharges=1.05
            ),
            trade(
                "BUY", 30, 110.0, brokerageCharges=20.0, stt=0.0, exchangeTransactionCharges=1.16
            ),
            trade(
                "SELL", 30, 120.0, brokerageCharges=20.0, stt=3.6, exchangeTransactionCharges=1.26
            ),
        ]
        cost_for = ScriptedCosts()
        out = charges_check(rows, cost_for)
        assert [(g["product"], g["side"]) for g in out] == [("index_options", "BUY"),
                                                           ("index_options", "SELL")]  # fmt: skip
        buy, sell = out
        assert buy["fills"] == 2 and buy["turnover"] == 30 * 100.0 + 30 * 110.0
        assert buy["actual"] == pytest.approx(20 + 1.05 + 20 + 1.16)
        assert buy["model"] == pytest.approx(3.0 + 1.0 + 3.3 + 1.0)
        assert sell["fills"] == 1 and sell["actual"] == pytest.approx(24.86)
        assert sell["model"] == pytest.approx(3.6 + 2.0)
        assert buy["actual_pct"] == pytest.approx(buy["actual"] / buy["turnover"] * 100)
        assert buy["model_pct"] == pytest.approx(8.3 / 6300 * 100)

    def test_the_cost_table_is_asked_for_the_product_price_quantity_and_lowercase_side(self):
        cost_for = ScriptedCosts()
        charges_check([trade("SELL", 15, 99.5)], cost_for)
        assert cost_for.calls == [("index_options", 99.5, 15, "sell")]

    def test_every_charge_is_summed_and_kept_by_part(self):
        row = trade(
            "BUY", 30, 100.0, brokerageCharges=20.0, stt=1.0, exchangeTransactionCharges=2.0,
            sebiTax=0.5, stampDuty=0.25, serviceTax=3.75,
        )  # fmt: skip
        (g,) = charges_check([row], ScriptedCosts())
        assert g["actual"] == pytest.approx(27.5)
        assert g["actual_parts"] == {
            "brokerageCharges": 20.0, "stt": 1.0, "exchangeTransactionCharges": 2.0,
            "sebiTax": 0.5, "stampDuty": 0.25, "serviceTax": 3.75,
        }  # fmt: skip
        assert set(g["actual_parts"]) == set(CHARGE_FIELDS)

    @pytest.mark.parametrize("missing", [None, "", 0, "0"])
    def test_a_charge_the_broker_left_blank_counts_as_nothing(self, missing):
        (g,) = charges_check([trade(brokerageCharges=20.0, stt=missing)], ScriptedCosts())
        assert g["actual"] == 20.0 and g["actual_parts"]["stt"] == 0.0

    def test_charges_sent_as_strings_are_read(self):
        (g,) = charges_check([trade(brokerageCharges="20.0", stt="0.55")], ScriptedCosts())
        assert g["actual"] == pytest.approx(20.55)

    def test_a_side_in_lowercase_is_still_a_side(self):
        out = charges_check([trade("buy"), trade("Sell")], ScriptedCosts())
        assert [(x["side"], x["fills"]) for x in out] == [("BUY", 1), ("SELL", 1)]

    @pytest.mark.parametrize(
        "bad",
        [
            {"tradedQuantity": 0}, {"tradedQuantity": -30}, {"tradedQuantity": None},
            {"tradedQuantity": "many"}, {"tradedQuantity": "1.5"},
            {"tradedPrice": 0}, {"tradedPrice": -1.0}, {"tradedPrice": None},
            {"tradedPrice": "n/a"},
            {"transactionType": "HOLD"}, {"transactionType": ""}, {"transactionType": None},
            {"exchangeSegment": "MCX_COMM"}, {"exchangeSegment": None},
        ],
    )  # fmt: skip
    def test_a_row_with_a_bad_quantity_price_side_or_product_is_skipped(self, bad):
        good = trade("BUY", 30, 100.0, brokerageCharges=20.0)
        out = charges_check([{**good, **bad}, good], ScriptedCosts())
        assert [(g["fills"], g["actual"]) for g in out] == [(1, 20.0)]

    @pytest.mark.parametrize("missing", ["tradedQuantity", "tradedPrice"])
    def test_a_row_missing_its_quantity_or_price_key_is_skipped(self, missing):
        row = {k: v for k, v in trade().items() if k != missing}
        assert charges_check([row], ScriptedCosts()) == []

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_price_that_is_not_a_finite_number_is_skipped_never_summed_into_nan(self, bad):
        """A NaN or infinite price is as bad as a missing one: it must not reach the sums (the
        report is JSON, and NaN is not JSON: one such row would take the endpoint down)."""
        good = trade("BUY", 30, 100.0, brokerageCharges=20.0)
        out = charges_check([{**good, "tradedPrice": bad}, good], ScriptedCosts())
        assert [(g["fills"], g["actual"]) for g in out] == [(1, 20.0)]
        json.dumps(out, allow_nan=False)

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 1e400])
    def test_a_quantity_that_is_not_a_finite_number_is_skipped_not_a_crash(self, bad):
        good = trade("BUY", 30, 100.0, brokerageCharges=20.0)
        out = charges_check([{**good, "tradedQuantity": bad}, good], ScriptedCosts())
        assert [(g["fills"], g["actual"]) for g in out] == [(1, 20.0)]

    def test_no_trades_is_an_empty_list(self):
        assert charges_check([], ScriptedCosts()) == []
        assert charges_check(iter(()), ScriptedCosts()) == []

    def test_groups_come_out_sorted_by_product_then_side(self):
        rows = [
            trade("SELL", seg="NSE_EQ", instrument="", productType="INTRADAY"),
            trade("BUY"),
            trade("BUY", seg="NSE_EQ", instrument="", productType="INTRADAY"),
            trade("SELL"),
        ]
        out = charges_check(rows, ScriptedCosts())
        assert [(g["product"], g["side"]) for g in out] == [
            ("equity_intraday", "BUY"), ("equity_intraday", "SELL"),
            ("index_options", "BUY"), ("index_options", "SELL"),
        ]  # fmt: skip

    def test_money_is_rounded_to_the_paisa_but_the_percentages_are_not(self):
        rows = [trade("BUY", 7, 33.333, brokerageCharges=0.123456, stt=0.0)]
        (g,) = charges_check(rows, ScriptedCosts())
        assert g["actual"] == 0.12 and g["turnover"] == round(7 * 33.333, 2)
        assert g["actual_pct"] == pytest.approx(0.123456 / (7 * 33.333) * 100)
        assert g["actual_parts"]["brokerageCharges"] == 0.12

    def test_the_real_cost_table_gives_a_model_figure_for_each_product(self):
        table = CostConfig.load(ROOT / "config" / "costs.yaml")
        rows = [
            trade("BUY", 30, 300.0), trade("SELL", 30, 310.0),
            trade("BUY", 100, 50.0, seg="NSE_EQ", instrument="", productType="INTRADAY"),
        ]  # fmt: skip
        out = charges_check(rows, lambda name: CostModel(table.product(name)))
        buy = next(g for g in out if g["product"] == "index_options" and g["side"] == "BUY")
        want = float(CostModel(table.product("index_options")).leg(300.0, 30, "buy").total)
        assert buy["model"] == pytest.approx(round(want, 2)) and want > 0
        eq = next(g for g in out if g["product"] == "equity_intraday")
        assert eq["model"] > 0 and eq["fills"] == 1


class TestDeskFillCharges:
    class Book:
        def __init__(self, rows=None, fail=False):
            self.rows, self.fail = rows if rows is not None else [], fail
            self.calls: list[tuple] = []

        def trade_history(self, start, end, max_pages=20):
            self.calls.append((start, end, max_pages))
            if self.fail:
                raise RuntimeError("DH-901 token expired")
            return list(self.rows)

    @pytest.fixture
    def rig(self, db, monkeypatch):
        desk, _, _ = new_desk(AppConfig.load(), db, desk_cfg=small_cfg())
        clock = {"now": dt.datetime(2026, 11, 6, 12, 0, tzinfo=IST)}
        monkeypatch.setattr("onelot.desk.desk.now_ist", lambda: clock["now"])
        return SimpleNamespace(desk=desk, clock=clock)

    @staticmethod
    def wire(rig, book):
        rig.desk._adapter_getter = lambda: book

    def test_without_a_broker_it_is_empty(self, rig):
        assert rig.desk.fill_charges() == {}

    def test_it_reads_the_trade_book_for_the_last_year_in_pages_of_sixty(self, rig):
        book = self.Book()
        self.wire(rig, book)
        rig.desk.fill_charges()
        ((start, end, pages),) = book.calls
        assert (start, end, pages) == (dt.date(2025, 11, 6), dt.date(2026, 11, 6), 60)

    def test_it_compares_billed_charges_with_the_cost_table_per_product_and_side(self, rig):
        rows = [
            trade(
                "BUY", 30, 300.0, brokerageCharges=20.0, stt=0.0, exchangeTransactionCharges=3.15
            ),
            trade(
                "SELL", 30, 310.0, brokerageCharges=20.0, stt=9.3, exchangeTransactionCharges=3.26
            ),
        ]
        self.wire(rig, self.Book(rows))
        out = rig.desk.fill_charges()
        assert out["days"] == 365 and out["fills"] == 2
        assert [(g["product"], g["side"]) for g in out["by_product"]] == [
            ("index_options", "BUY"), ("index_options", "SELL"),
        ]  # fmt: skip
        sell = out["by_product"][1]
        assert sell["actual"] == pytest.approx(32.56) and sell["model"] > 0
        json.dumps(out)

    def test_the_answer_is_kept_for_an_hour(self, rig):
        book = self.Book([trade(brokerageCharges=20.0)])
        self.wire(rig, book)
        first = rig.desk.fill_charges()
        rig.clock["now"] += dt.timedelta(minutes=59)
        assert rig.desk.fill_charges() is first and len(book.calls) == 1
        rig.clock["now"] += dt.timedelta(minutes=2)
        rig.desk.fill_charges()
        assert len(book.calls) == 2

    def test_a_different_window_is_not_served_from_the_cache(self, rig):
        book = self.Book([trade(brokerageCharges=20.0)])
        self.wire(rig, book)
        rig.desk.fill_charges(365)
        rig.desk.fill_charges(30)
        assert len(book.calls) == 2 and book.calls[1][0] == dt.date(2026, 10, 7)
        assert rig.desk.fill_charges(30)["days"] == 30 and len(book.calls) == 2

    def test_a_trade_book_error_comes_out_and_is_not_cached(self, rig):
        book = self.Book(fail=True)
        self.wire(rig, book)
        with pytest.raises(RuntimeError, match="DH-901"):
            rig.desk.fill_charges()
        book.fail = False
        assert rig.desk.fill_charges()["fills"] == 0 and len(book.calls) == 2

    def test_an_empty_trade_book_is_an_empty_answer_not_an_error(self, rig):
        self.wire(rig, self.Book([]))
        assert rig.desk.fill_charges() == {"days": 365, "fills": 0, "by_product": []}

    def test_the_endpoint_serves_it_through_a_real_desk(self, rig):
        self.wire(rig, self.Book([trade(brokerageCharges=20.0)]))
        body = client_for(rig.desk).get("/api/desk/fill-costs").json()
        assert body["charges"]["fills"] == 1 and body["charges"]["by_product"][0]["actual"] == 20.0


# ══ the desk's wiring ═════════════════════════════════════════════════════════════════════


class TestTheDesksRecorder:
    @pytest.fixture
    def desk(self, db):
        d, _, _ = new_desk(AppConfig.load(), db, desk_cfg=small_cfg())
        return d

    def test_it_samples_the_roll_window_the_alerts_use(self, db):
        d, _, _ = new_desk(AppConfig.load(), db, desk_cfg=small_cfg(option_roll_days=5))
        assert d.fillcost.roll_days == 5

    def test_it_watches_every_tracked_stock_and_index_with_a_security_id(self, desk):
        stocks = desk._fill_stocks()
        assert [(w.instrument_key, w.instrument_type, w.segment) for w in stocks] == [
            ("SBIN", "EQUITY", "NSE_EQ")
        ]
        assert stocks[0].security_id == "3045" and stocks[0].label == "SBIN"
        assert desk._fill_indices() == [("BANKNIFTY", "25", "NSE", "BANKNIFTY_OPT")]

    def test_the_market_is_open_when_the_session_window_says_so(self, desk):
        assert desk._market_open(NOW) is True
        from desk_harness import ShutClock

        desk.session_clock = ShutClock()
        assert desk._market_open(NOW) is False
        desk.session_clock = SimpleNamespace(current_window=lambda ts=None: None)
        assert desk._market_open(NOW) is False

    def test_the_desk_starts_and_stops_the_recorder_with_itself(self, desk, monkeypatch):
        events = []
        monkeypatch.setattr(desk.fillcost, "start", lambda: events.append("start"))
        monkeypatch.setattr(desk.fillcost, "stop", lambda: events.append("stop"))
        monkeypatch.setattr(desk, "_loop", lambda: None)
        desk.start()
        desk._thread.join(timeout=5)
        desk.stop()
        assert events == ["start", "stop"]


# ══ the command line ══════════════════════════════════════════════════════════════════════


class TestFillCostsCommand:
    @staticmethod
    def secrets(creds: bool):
        return SimpleNamespace(
            has_broker_creds=creds, client_id="1100001234", access_token="TOKEN-SECRET-VALUE"
        )

    @staticmethod
    def cfg():
        return SimpleNamespace(db_url_resolved="sqlite://", project_root=ROOT)

    def seeded(self, engine, monkeypatch):
        for i, hs in enumerate([0.05, 0.10, 0.15]):
            sample(engine, dt.datetime.now(IST) - dt.timedelta(minutes=i + 1), "RELIANCE", "2885",
                   100 - hs, 100 + hs)  # fmt: skip
        monkeypatch.setattr("onelot.data.db.get_engine", lambda url=None: engine)

    def test_the_parser_knows_the_command_and_defaults_to_thirty_days(self):
        from onelot.__main__ import build_parser

        parser = build_parser()
        assert parser.parse_args(["fill-costs"]).days == 30
        assert parser.parse_args(["fill-costs", "--days", "7"]).days == 7
        assert parser.parse_args(["fill-costs"]).cmd == "fill-costs"

    def test_it_prints_the_measured_spreads_against_the_labs_assumptions(
        self, engine, monkeypatch, capsys
    ):
        from onelot.__main__ import cmd_fill_costs

        self.seeded(engine, monkeypatch)
        rc = cmd_fill_costs(SimpleNamespace(days=30), self.cfg(), self.secrets(False))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Fill costs, last 30 days: 3 quote samples" in out
        assert "stocks : measured half-spread 0.100% per side vs the Lab's 0.03% slippage" in out
        assert "options: measured half-spread - per side" in out
        assert "RELIANCE" in out and "n=3" in out and "median 0.100%" in out

    def test_without_broker_credentials_the_trade_book_is_never_touched(
        self, engine, monkeypatch, capsys
    ):
        from onelot.__main__ import cmd_fill_costs

        self.seeded(engine, monkeypatch)
        built = []
        monkeypatch.setattr("onelot.broker.dhan.DhanAdapter", lambda *a, **k: built.append(a))
        cmd_fill_costs(SimpleNamespace(days=30), self.cfg(), self.secrets(False))
        assert built == [] and "Charges on your own fills" not in capsys.readouterr().out

    def test_with_credentials_it_prints_the_billed_charges_against_the_cost_table(
        self, engine, monkeypatch, capsys
    ):
        from onelot.__main__ import cmd_fill_costs

        self.seeded(engine, monkeypatch)
        rows = [trade("BUY", 30, 300.0, brokerageCharges=20.0, exchangeTransactionCharges=3.15)]

        class Adapter:
            def __init__(self, client_id, token, limiter):
                self.args = (client_id, token)

            def trade_history(self, start, end, pages):
                assert (end - start).days == 365 and pages == 60
                return rows

        monkeypatch.setattr("onelot.broker.dhan.DhanAdapter", Adapter)
        cmd_fill_costs(SimpleNamespace(days=30), self.cfg(), self.secrets(True))
        out = capsys.readouterr().out
        assert "Charges on your own fills (Dhan trade book, 365 days, 1 fills):" in out
        assert "index_options" in out and "BUY" in out and "billed 23.15" in out
        assert "TOKEN-SECRET-VALUE" not in out

    def test_a_trade_book_that_fails_is_a_line_not_a_crash_and_leaks_nothing(
        self, engine, monkeypatch, capsys
    ):
        from onelot.__main__ import cmd_fill_costs

        self.seeded(engine, monkeypatch)

        class Adapter:
            def __init__(self, *a):
                pass

            def trade_history(self, *a):
                raise RuntimeError("DH-901 TOKEN-SECRET-VALUE rejected")

        monkeypatch.setattr("onelot.broker.dhan.DhanAdapter", Adapter)
        rc = cmd_fill_costs(SimpleNamespace(days=30), self.cfg(), self.secrets(True))
        out = capsys.readouterr().out
        assert rc == 0 and "trade book unavailable (RuntimeError)" in out
        assert "TOKEN-SECRET-VALUE" not in out

    def test_an_empty_database_prints_the_header_and_no_rows(self, engine, monkeypatch, capsys):
        from onelot.__main__ import cmd_fill_costs

        monkeypatch.setattr("onelot.data.db.get_engine", lambda url=None: engine)
        assert cmd_fill_costs(SimpleNamespace(days=30), self.cfg(), self.secrets(False)) == 0
        out = capsys.readouterr().out
        assert "0 quote samples" in out and "measured half-spread -" in out

    def test_main_routes_the_command_to_it_without_reading_any_credentials(self, monkeypatch):
        from onelot import __main__ as cli

        seen = {}
        stub_cfg = SimpleNamespace(
            project_root=ROOT, log_dir=ROOT / "logs",
            logging=SimpleNamespace(level="INFO", max_bytes=1_000_000, backup_count=1),
        )  # fmt: skip
        monkeypatch.setattr(cli.AppConfig, "load", classmethod(lambda cls, *a, **k: stub_cfg))
        monkeypatch.setattr(
            cli.Secrets, "load",
            classmethod(lambda cls, root: SimpleNamespace(secret_values=lambda: [])),
        )  # fmt: skip
        monkeypatch.setattr(cli, "setup_logging", lambda **k: seen.setdefault("log", k))
        monkeypatch.setattr(
            cli, "cmd_fill_costs", lambda args, cfg, secrets: seen.setdefault("days", args.days) * 0
        )
        assert cli.main(["fill-costs", "--days", "9"]) == 0
        assert seen["days"] == 9 and seen["log"]["component"] == "fill-costs"
