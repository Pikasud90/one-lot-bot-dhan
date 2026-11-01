"""US-001 mode B: the Engine builds the PaperBook from config and attaches it to the Desk.

Production wiring (AC8, AC7): `Engine._paper_book()` -> `onelot.paper.factory.build_paper_book`
-> `Desk(..., paper=book)` in `Engine.start`. `Engine.start` itself needs a broker, a lock
and threads, so it cannot run in a unit test; what is driven here is everything it relies on,
with a REAL `Engine` (mock broker, never started) over a throw-away copy of config/ and a tmp
SQLite file, and a real Desk polling an offline bar source:

* the book it builds carries the config's paper/backtest sections, the real instrument specs,
  the real cost tables (costs.yaml parsed once) and a Black-76 pricer that agrees with the Lab;
* a book that cannot be built never stops the desk (logged, warned, desk runs without it), and
  `paper.enabled: false` still attaches a book so trades already open keep being managed;
* `Engine.start` hands the book to the Desk (static check of the call);
* end to end: stock and index alerts fill through the Desk at prices an independent oracle gives;
* the desk snapshot's `paper` block (the book's status() beside the desk's own hook errors).
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import math

import pytest
import us001_helpers as H
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.config import CostConfig
from onelot.costs import CostModel, round_to_tick


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def eng(tmp_path):
    with H.real_engine(tmp_path) as engine:
        yield engine


def first_bar_after(frame, ts):
    return frame.index[frame.index > ts][0]


class TestTheEngineBuildsTheBook:
    def test_the_book_carries_the_engines_config_database_and_real_specs(self, eng):
        book = eng._paper_book()
        assert type(book) is H.paper_book_class()
        assert book.cfg == eng.cfg.paper and book.bt == eng.cfg.backtest
        assert book.engine is eng.db.engine
        assert eng.status.warnings == []
        assert {"paper_trades", "paper_events"} <= set(eng.db.table_names())
        sbin, option = book.specs["SBIN"], book.specs["BANKNIFTY_OPT"]
        assert (sbin.category, sbin.lot_size, sbin.product) == ("STOCK", 1, "equity_intraday")
        assert (option.category, option.product) == ("OPTIONS", "index_options")
        assert option.lot_size > 1 and option.tick_size == 0.05

    def test_each_call_builds_a_book_on_the_same_database_without_duplicates(self, eng):
        first = eng._paper_book()
        H.open_trade(first, alert_id="A1")
        second = eng._paper_book()  # what a second Engine start does
        assert [t.alert_id for t in second.open_trades()] == ["A1"]
        assert second.status()["opened"] == 0  # nothing opened by this book itself
        assert H.open_trade(second, alert_id="A1").id == first.open_trades()[0].id
        assert len(H.trade_rows(eng.db.engine)) == 1

    def test_costs_yaml_is_parsed_once_per_build_and_one_model_serves_a_product(
        self, eng, monkeypatch
    ):
        real = CostConfig.load.__func__  # type: ignore[attr-defined]
        loads: list[object] = []

        def counting(cls, path):
            loads.append(path)
            return real(cls, path)

        monkeypatch.setattr(CostConfig, "load", classmethod(counting))
        book = eng._paper_book()
        assert len(loads) == 1
        models = [book.cost_for(k) for k in ("SBIN", "RELIANCE", "SBIN", "BANKNIFTY_OPT")]
        assert len(loads) == 1, "costs.yaml was parsed again inside cost_for"
        assert all(isinstance(m, CostModel) for m in models)
        assert models[0] is models[1] is models[2]  # one equity_intraday model
        assert models[3] is not models[0]  # and an index_options one
        reference = CostConfig.load.__func__(  # type: ignore[attr-defined]
            CostConfig, eng.cfg.project_root / "config" / "costs.yaml"
        )
        for key, product in (("SBIN", "equity_intraday"), ("BANKNIFTY_OPT", "index_options")):
            mine = book.cost_for(key).leg(1234.5, 7, "sell")
            theirs = CostModel(reference.product(product)).leg(1234.5, 7, "sell")
            assert mine.total == theirs.total

    def test_an_instrument_without_a_spec_is_a_key_error_not_a_wrong_cost_table(self, eng):
        with pytest.raises(KeyError):
            eng._paper_book().cost_for("NOT_A_REAL_INSTRUMENT")

    @pytest.mark.parametrize("opt_type", ["CE", "PE"])
    @pytest.mark.parametrize("underlying", [53000.0, 54000.0, 55250.5])
    @pytest.mark.parametrize("iv", [0.12, 0.2, 0.55])
    def test_the_option_pricer_agrees_with_the_labs_black_76(self, eng, opt_type, underlying, iv):
        book = eng._paper_book()
        option = {"strike": 54000.0, "opt_type": opt_type, "expiry": "2026-11-24", "iv": iv}
        ts = H.at(10, 6)
        mine = book.premium_at(option, underlying, ts)
        theirs = H.lab_fair_premium(
            strike=54000.0,
            opt_type=opt_type,
            expiry="2026-11-24",
            underlying=underlying,
            ts=ts,
            iv=iv,
            rate=eng.cfg.backtest.risk_free_rate,
        )
        assert mine == pytest.approx(theirs, rel=1e-9)

    @pytest.mark.parametrize(
        "changes",
        [
            {"iv": None},
            {"iv": 0.0},
            {"iv": -0.2},
            {"iv": float("nan")},
            {"strike": None},
            {"strike": -1.0},
            {"opt_type": "XX"},
            {"expiry": None},
            {"expiry": "next friday"},
        ],
    )
    def test_the_pricer_says_none_when_any_input_is_unusable_never_a_guess(self, eng, changes):
        option = {"strike": 54000.0, "opt_type": "CE", "expiry": "2026-11-24", "iv": 0.2} | changes
        assert eng._paper_book().premium_at(option, 54000.0, H.at(10, 6)) is None

    @pytest.mark.parametrize("underlying", [0.0, -5.0, float("nan"), float("inf"), None])
    def test_the_pricer_says_none_for_an_unusable_underlying(self, eng, underlying):
        option = {"strike": 54000.0, "opt_type": "CE", "expiry": "2026-11-24", "iv": 0.2}
        assert eng._paper_book().premium_at(option, underlying, H.at(10, 6)) is None

    def test_the_pricer_refuses_a_naive_timestamp(self, eng):
        option = {"strike": 54000.0, "opt_type": "CE", "expiry": "2026-11-24", "iv": 0.2}
        naive = H.at(10, 6).replace(tzinfo=None)
        assert eng._paper_book().premium_at(option, 54000.0, naive) is None


ANYTHING = st.one_of(
    st.none(),
    st.booleans(),
    st.text(max_size=8),
    st.integers(-(10**12), 10**12),
    st.floats(allow_nan=True, allow_infinity=True),
    st.just(float("nan")),
    st.lists(st.integers(), max_size=2),
)
PRICING_PROP = settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)


class TestThePricerIsTotal:
    """premium_at runs inside the Desk thread on every fill: for ANY input it must answer with a
    finite positive price or None, never raise (the book would count it, but a silent None is the
    contract: 'any missing or invalid input gives None, never a guess')."""

    @given(
        strike=ANYTHING,
        opt_type=st.sampled_from(["CE", "PE", "XX", "", None]),
        expiry=st.one_of(st.none(), st.text(max_size=12), st.dates().map(lambda d: d.isoformat())),
        iv=ANYTHING,
        underlying=ANYTHING,
        minutes=st.integers(-60 * 24 * 400, 60 * 24 * 400),
    )
    @PRICING_PROP
    def test_property_black76_answers_a_positive_finite_price_or_none(
        self, strike, opt_type, expiry, iv, underlying, minutes
    ):
        from onelot.paper.pricing import black76_premium

        option = {"strike": strike, "opt_type": opt_type, "expiry": expiry, "iv": iv}
        ts = H.at(10, 6) + dt.timedelta(minutes=minutes)
        out = black76_premium(option, underlying, ts, 0.065)
        assert out is None or (isinstance(out, float) and math.isfinite(out) and out > 0), out

    @given(
        premium=ANYTHING,
        underlying=ANYTHING,
        strike=st.one_of(ANYTHING, st.floats(1.0, 1e6)),
        days=st.integers(-3, 400),
        opt_type=st.sampled_from(["CE", "PE"]),
    )
    @PRICING_PROP
    def test_property_implied_iv_is_none_or_strictly_inside_the_bounds(
        self, premium, underlying, strike, days, opt_type
    ):
        from onelot.paper.pricing import IV_CEILING, IV_FLOOR, implied_iv

        expiry = (H.DAY + dt.timedelta(days=days)).isoformat()
        option = {"strike": strike, "opt_type": opt_type, "expiry": expiry}
        out = implied_iv(option, premium, underlying, H.CREATED, 0.065)
        assert out is None or (math.isfinite(out) and IV_FLOOR < out < IV_CEILING), out

    def test_a_premium_after_expiry_is_positive_intrinsic_or_none(self):
        from onelot.paper.pricing import black76_premium

        option = {"strike": 54000.0, "opt_type": "CE", "expiry": "2026-10-29", "iv": 0.2}
        after_close = H.at(15, 45)  # expiry is 15:30 on that day
        itm = black76_premium(option, 54500.0, after_close, 0.065)
        otm = black76_premium(option, 53000.0, after_close, 0.065)
        assert itm == pytest.approx(500.0, abs=2.0) and (otm is None or otm < 1e-6)


class TestTheEngineHandsTheBookToTheDesk:
    def test_start_passes_the_book_to_the_desk_it_builds(self):
        """Engine.start needs a broker, a lock and threads; the call that wires the book is
        checked in the source: `Desk(..., paper=self._paper_book())` inside `Engine.start`."""
        tree = ast.parse((H.ROOT / "src" / "onelot" / "engine.py").read_text(encoding="utf-8"))
        start = next(
            n
            for c in ast.walk(tree)
            if isinstance(c, ast.ClassDef) and c.name == "Engine"
            for n in c.body
            if isinstance(n, ast.FunctionDef) and n.name == "start"
        )
        desk_calls = [
            n
            for n in ast.walk(start)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Desk"
        ]
        assert len(desk_calls) == 1, "Engine.start must build exactly one Desk"
        (paper,) = [k for k in desk_calls[0].keywords if k.arg == "paper"]
        call = paper.value
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        assert call.func.attr == "_paper_book" and getattr(call.func.value, "id", "") == "self"

    def test_a_book_that_fails_to_build_is_logged_and_the_desk_runs_without_it(
        self, eng, monkeypatch, caplog
    ):
        def explode(*_a, **_k):
            raise RuntimeError("costs.yaml is unreadable")

        monkeypatch.setattr("onelot.paper.factory.build_paper_book", explode)
        with caplog.at_level(logging.ERROR):
            book = eng._paper_book()  # must not raise
        assert book is None
        assert any("Paper book failed to build" in w for w in eng.status.warnings)
        assert any("paper book failed to build" in r.getMessage() for r in caplog.records)
        d, notifier = H.new_desk(eng.cfg, eng.db, paper=book)
        d.poll()
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        d.poll()
        assert d.paper is None and notifier.sent[-1].signal_id == alert["id"]
        assert H.count_rows(eng.db.engine, "paper_trades") == 0
        assert d.snapshot()["paper"] is None

    def test_a_missing_cost_table_is_survived_the_same_way(self, eng, tmp_path):
        eng.cfg = eng.cfg.model_copy(update={"project_root": tmp_path / "nowhere"})
        assert eng._paper_book() is None
        assert any("Paper book failed to build" in w for w in eng.status.warnings)

    def test_a_disabled_config_still_attaches_a_book_so_open_trades_keep_running(self, tmp_path):
        """paper.enabled false stops NEW trades only (AC7): the Engine must still build and attach
        a book, or a trade opened before the switch would wait forever."""
        with H.real_engine(tmp_path, enabled=False) as eng:
            earlier = H.desk_book(
                eng.cfg, eng.db.engine, enabled=True
            )  # the book before the switch
            d0, _ = H.new_desk(eng.cfg, eng.db, paper=earlier)
            d0.poll()
            waiting = H.raise_alert(d0, "SBIN", now=H.mid_session(d0, "SBIN"))
            assert [H.val(r["status"]) for r in H.trade_rows(eng.db.engine)] == ["PENDING_FILL"]

            book = eng._paper_book()
            assert book is not None and book.cfg.enabled is False
            assert [t.alert_id for t in book.open_trades()] == [waiting["id"]]
            d1, notifier = H.new_desk(eng.cfg, eng.db, paper=book)
            d1.poll()  # the disabled book still fills what is waiting
            assert H.val(H.trade_row(eng.db.engine, waiting["id"])["status"]) == "OPEN"
            new_alert = H.raise_alert(d1, "BANKNIFTY", now=H.mid_session(d1, "BANKNIFTY"))
            d1.poll()  # ... and opens nothing new, without a hook error
            assert [r["alert_id"] for r in H.trade_rows(eng.db.engine)] == [waiting["id"]]
            assert d1.hook_errors == 0 and notifier.sent[-1].signal_id == new_alert["id"]


class TestACrashBetweenTheInsertAndTheHookIsRepairedByTheEnginesOwnBook:
    def test_the_next_start_books_the_alert_the_desk_stored_without_a_book(self, eng):
        crashed, _ = H.new_desk(eng.cfg, eng.db)  # an alert raised while no book was attached
        crashed.poll()
        alert = H.raise_alert(crashed, "SBIN", now=H.mid_session(crashed, "SBIN"))
        assert H.count_rows(eng.db.engine, "paper_trades") == 0
        book = eng._paper_book()  # the restart: Engine builds its book, which reconciles
        row = H.trade_row(eng.db.engine, alert["id"])
        assert H.val(row["status"]) == "PENDING_FILL" and book.status()["opened"] == 1
        d, _ = H.new_desk(eng.cfg, eng.db, paper=book)
        d.poll()  # and the restarted desk fills it from the bars it holds
        row = H.trade_row(eng.db.engine, alert["id"])
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] is not None
        assert [H.val(e["kind"]) for e in H.event_rows(eng.db.engine, alert["id"])] == [
            "OPENED",
            "FILLED",
        ]


class TestEndToEndThroughTheEngineBuiltBook:
    def test_a_stock_alert_fills_on_the_next_poll_at_the_oracle_price(self, eng):
        d, _ = H.new_desk(eng.cfg, eng.db, paper=eng._paper_book())
        d.poll()
        now = H.mid_session(d, "SBIN")
        alert = H.raise_alert(d, "SBIN", now=now)
        d.poll()
        bars = d.trackers["SBIN"].bars
        fill_ts = first_bar_after(bars, now)
        row = H.trade_row(eng.db.engine, alert["id"])
        assert H.val(row["status"]) == "OPEN"
        assert H.same_instant(row["fill_bar_ts"], fill_ts.to_pydatetime())
        assert row["entry_paise"] == H.expected_stock_entry(
            float(bars["open"].loc[fill_ts]), "long", eng.cfg.backtest.slippage_pct
        )
        assert row["qty"] == 1 and row["product"] == "equity_intraday"
        stop = H.to_paise(alert["economics"]["underlying_stop"])
        assert row["risk_paise"] == abs(row["entry_paise"] - stop)

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_an_index_alert_fills_at_the_lab_priced_premium_with_the_iv_frozen_at_the_alert(
        self, eng, direction
    ):
        d, _ = H.new_desk(eng.cfg, eng.db, paper=eng._paper_book())
        d.poll()
        now = H.mid_session(d, "BANKNIFTY")
        alert = H.raise_alert(d, "BANKNIFTY", now=now, direction=direction)
        d.poll()
        option = alert["economics"]["option"]
        bars = d.trackers["BANKNIFTY"].bars
        fill_ts = first_bar_after(bars, now)
        bt = eng.cfg.backtest
        tick, lot = 0.05, int(option["lot"])
        fair = H.lab_fair_premium(
            strike=option["strike"],
            opt_type=option["opt_type"],
            expiry=option["expiry"],
            underlying=H.to_paise(float(bars["open"].loc[fill_ts])) / 100,  # priced at paise
            ts=fill_ts.to_pydatetime(),
            iv=option["iv"],
            rate=bt.risk_free_rate,
        )
        entry = H.to_paise(
            round_to_tick(
                fair * (1 + bt.option_spread_pct) + bt.option_slippage_ticks * tick, tick, "up"
            )
        )
        stop_fair = H.lab_fair_premium(
            strike=option["strike"],
            opt_type=option["opt_type"],
            expiry=option["expiry"],
            underlying=H.to_paise(alert["economics"]["underlying_stop"]) / 100,
            ts=fill_ts.to_pydatetime(),
            iv=option["iv"],
            rate=bt.risk_free_rate,
        )
        sold = round_to_tick(
            stop_fair * (1 - bt.option_spread_pct) - bt.option_slippage_ticks * tick, tick, "down"
        )
        risk = (entry - H.to_paise(max(sold, tick))) * lot
        row = H.trade_row(eng.db.engine, alert["id"])
        assert H.val(row["status"]) == "OPEN", row
        assert row["entry_paise"] == entry
        assert row["risk_paise"] == risk
        assert row["qty"] == lot and row["product"] == "index_options"
        stored = row["plan"]["alert"]["economics"]["option"]["iv"]
        assert stored == option["iv"], "the IV stored in the plan is the alert's, unchanged"


class TestTheDeskSnapshotsTheBook:
    def test_the_paper_block_is_the_books_status_beside_the_desks_own_errors(self, eng):
        book = eng._paper_book()
        d, _ = H.new_desk(eng.cfg, eng.db, paper=book)
        d.poll()
        H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        d.poll()
        block = d.snapshot()["paper"]
        assert block == {**book.status(), "desk_hook_errors": 0}
        assert block["opened"] == 1 and block["filled"] == 1
        assert set(block) == {
            "opened",
            "filled",
            "no_fill",
            "closed",
            "hook_errors",
            "desk_hook_errors",
        }
        assert json.loads(json.dumps(d.snapshot()["paper"])) == block  # SSE-safe

    def test_the_block_is_null_without_a_book(self, eng):
        d, _ = H.new_desk(eng.cfg, eng.db)
        d.poll()
        assert d.snapshot()["paper"] is None
        assert d.hook_errors == 0

    def test_a_status_that_raises_is_counted_and_the_snapshot_survives(self, eng):
        class BrokenStatus(H.SpyPaper):
            def status(self):
                raise RuntimeError("status exploded")

        d, _ = H.new_desk(eng.cfg, eng.db, paper=BrokenStatus())
        before = d.version
        d.poll()
        d.poll()
        assert d.version > before and d.snapshot()["updated_at"] is not None
        assert d.snapshot()["paper"] == {"desk_hook_errors": d.hook_errors}
        assert d.hook_errors >= 2

    def test_the_api_serves_the_paper_block_with_the_desk(self, eng):
        import warnings
        from unittest.mock import MagicMock

        from onelot.api.server import create_app

        with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient

        d, _ = H.new_desk(eng.cfg, eng.db, paper=eng._paper_book())
        d.poll()
        stub = MagicMock()
        stub.desk = d
        body = TestClient(create_app(stub, "test-token")).get("/api/desk").json()
        assert body["paper"]["opened"] == 0 and body["paper"]["desk_hook_errors"] == 0
