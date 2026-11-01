"""Production wiring (US-001 AC8; Manager clarification): the Engine builds the PaperBook from
config and hands it to the Desk — developer tests for T4.

    onelot.paper.factory.build_paper_book(app_cfg, engine, clock=now_ist) -> PaperBook
        cfg  = app_cfg.paper, bt = app_cfg.backtest, specs = load_specs() instruments
        cost_for(instrument_key): the costs.yaml table of that spec's product, parsed ONCE
        premium_at(option, underlying, ts): Black-76 with the alert's frozen strike, expiry,
            opt_type and IV (ADR-001 amendment 6); None when any input is missing, never a guess
    Engine._paper_book() -> PaperBook | None   (None + logged + a status warning on failure)
    Engine.start: Desk(..., paper=self._paper_book())
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import logging
from pathlib import Path

import pytest

from onelot.analytics.blackscholes import price as b76_price
from onelot.config import AppConfig, CostConfig, PaperCfg
from onelot.lab.execution import expiry_datetime, year_frac
from onelot.paper.book import PaperBook

from .conftest import THU, ist, make_alert

ROOT = Path(__file__).resolve().parents[2]
OPTION = {"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24", "iv": 0.1532, "lot": 30}


@pytest.fixture
def app_cfg() -> AppConfig:
    return AppConfig.load(ROOT / "config" / "config.yaml", project_root=ROOT)


def build(app_cfg, engine, **kw):
    from onelot.paper.factory import build_paper_book

    return build_paper_book(app_cfg, engine, clock=lambda: ist(THU, 10, 8), **kw)


# ── build_paper_book ─────────────────────────────────────────────────────────────


def test_the_book_is_built_from_the_app_config(app_cfg, engine):
    book = build(app_cfg, engine)
    assert isinstance(book, PaperBook)
    assert book.cfg == app_cfg.paper and book.bt == app_cfg.backtest
    assert {"BANKNIFTY_OPT", "NIFTY_OPT", "SBIN", "RELIANCE"} <= set(book.specs)
    assert book.clock() == ist(THU, 10, 8)


def test_cost_tables_are_parsed_once_and_follow_the_spec_product(app_cfg, engine, monkeypatch):
    real = CostConfig.load.__func__  # type: ignore[attr-defined]
    calls: list[object] = []

    def counting(cls, path):
        calls.append(path)
        return real(cls, path)

    monkeypatch.setattr(CostConfig, "load", classmethod(counting))
    book = build(app_cfg, engine)
    stock, option = book.cost_for("SBIN"), book.cost_for("BANKNIFTY_OPT")
    assert book.cost_for("BANKNIFTY_OPT") is option and book.cost_for("SBIN") is stock
    assert len(calls) <= 1, f"costs.yaml parsed {len(calls)} times"

    base = real(CostConfig, ROOT / "config" / "costs.yaml")
    assert stock.cfg.taxes == base.product("equity_intraday").taxes
    assert option.cfg.taxes == base.product("index_options").taxes


@pytest.mark.parametrize("opt_type", ["CE", "PE"])
def test_premium_at_is_black76_with_the_iv_frozen_on_the_alert(app_cfg, engine, opt_type):
    book = build(app_cfg, engine)
    option = OPTION | {"opt_type": opt_type}
    ts, u = ist(THU, 10, 6), 45224.65
    t = year_frac(ts, expiry_datetime(dt.date(2026, 11, 24)))
    want = b76_price(u, 45000.0, t, 0.1532, app_cfg.backtest.risk_free_rate, opt_type)
    assert book.premium_at(option, u, ts) == pytest.approx(want, rel=1e-12)
    assert book.premium_at(option | {"iv": 0.30}, u, ts) > want  # the alert's IV, not VIX's


@pytest.mark.parametrize(
    "broken",
    [
        {"iv": None},
        {"iv": 0.0},
        {"iv": float("nan")},
        {"expiry": None},
        {"expiry": "not-a-date"},
        {"strike": None},
        {"opt_type": "XX"},
    ],
)
def test_premium_at_never_guesses_a_missing_input(app_cfg, engine, broken):
    book = build(app_cfg, engine)
    assert book.premium_at(OPTION | broken, 45224.65, ist(THU, 10, 6)) is None


def test_a_disabled_config_builds_a_book_that_opens_nothing(app_cfg, engine):
    cfg = app_cfg.model_copy(update={"paper": PaperCfg(enabled=False)})
    book = build(cfg, engine)
    assert book.on_alert(make_alert(), ist(THU, 10, 5, 4)) is None


# ── the Engine attaches it to the Desk ───────────────────────────────────────────


def bare_engine(app_cfg, tmp_path):
    from onelot.engine import Engine, EngineStatus
    from onelot.store import init_database

    eng = Engine.__new__(Engine)  # nothing started: no lock file, no broker, no threads
    eng.cfg = app_cfg
    eng.db = init_database(f"sqlite:///{tmp_path / 'engine.db'}")
    eng.status = EngineStatus(mode=app_cfg.mode)
    return eng


def test_the_engine_builds_its_paper_book_from_its_config(app_cfg, tmp_path):
    eng = bare_engine(app_cfg, tmp_path)
    try:
        book = eng._paper_book()
        assert isinstance(book, PaperBook) and book.cfg == app_cfg.paper
        assert book.engine is eng.db.engine
    finally:
        eng.db.dispose()


def test_a_paper_book_that_fails_to_build_is_logged_and_the_desk_runs_without_it(
    app_cfg, tmp_path, monkeypatch, caplog
):
    def boom(*_a, **_k):
        raise RuntimeError("costs.yaml unreadable")

    factory = importlib.import_module("onelot.paper.factory")
    monkeypatch.setattr(factory, "build_paper_book", boom)
    monkeypatch.setattr(
        importlib.import_module("onelot.engine"), "build_paper_book", boom, raising=False
    )
    eng = bare_engine(app_cfg, tmp_path)
    try:
        with caplog.at_level(logging.ERROR, logger="onelot.engine"):
            assert eng._paper_book() is None
        assert any("paper" in r.getMessage().lower() for r in caplog.records)
        assert any("paper" in w.lower() for w in eng.status.warnings)
    finally:
        eng.db.dispose()


def test_engine_start_hands_the_book_to_the_desk():
    """Engine.start cannot run in a unit test (broker, lock file, threads), so the call site
    is checked: the one Desk(...) call passes paper=self._paper_book()."""
    tree = ast.parse((ROOT / "src" / "onelot" / "engine.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Desk"
    ]
    assert len(calls) == 1, "expected exactly one Desk(...) call in engine.py"
    paper = [k.value for k in calls[0].keywords if k.arg == "paper"]
    assert paper, "Desk(...) in engine.py is not given paper="
    (value,) = paper
    assert isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
    assert value.func.attr == "_paper_book"
    assert isinstance(value.func.value, ast.Name) and value.func.value.id == "self"
