"""US-001 non-functional requirements and the public contract.

* tables `paper_trades` / `paper_events` live in `onelot.store.schema` (so create_all builds
  them): integer paise as BIGINT, `alert_id` UNIQUE, timestamps stored as UTC like the rest;
* config: `paper: {enabled: true, feed_delay_minutes: 15}`, a Strict model, in the YAML;
* the constructor / method signatures of the contract, frozen PaperTrade / PaperEvent;
* hermetic: injected dependencies only (never `current_settings()`, no per-call costs.yaml
  parse, no network, no governor), bars() read once per series per step, and a 45-trade
  x 30,000-row step that stays far inside its budget (proxy assertions, not a stopwatch test).
"""

from __future__ import annotations

import ast
import inspect
import time
from dataclasses import fields, is_dataclass

import pandas as pd
import pytest
import us001_helpers as H
import yaml
from pydantic import ValidationError
from sqlalchemy import BigInteger
from sqlalchemy import inspect as sa_inspect

from onelot.config import AppConfig, CostConfig, PaperCfg
from onelot.store.schema import METADATA, TZDateTime


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


MONEY_COLUMNS = (
    "entry_paise",
    "exit_paise",
    "gross_paise",
    "costs_paise",
    "net_paise",
    "risk_paise",
)
TIME_COLUMNS = ("created_at", "expires_at", "entry_ts", "fill_bar_ts", "last_bar_ts")


class TestTables:
    def test_both_tables_are_part_of_the_schema_metadata(self):
        assert {"paper_trades", "paper_events"} <= set(METADATA.tables)

    def test_create_all_builds_them_in_a_fresh_database(self, db):
        assert {"paper_trades", "paper_events"} <= set(db.table_names())

    @pytest.mark.parametrize("column", MONEY_COLUMNS)
    def test_money_is_stored_as_bigint_paise(self, column):
        assert isinstance(METADATA.tables["paper_trades"].c[column].type, BigInteger)

    @pytest.mark.parametrize("column", TIME_COLUMNS)
    def test_timestamps_use_the_schemas_utc_datetime_type(self, column):
        assert isinstance(METADATA.tables["paper_trades"].c[column].type, TZDateTime)
        assert isinstance(METADATA.tables["paper_events"].c["ts"].type, TZDateTime)

    def test_alert_id_is_unique(self, db):
        inspector = sa_inspect(db.engine)
        unique = [c["column_names"] for c in inspector.get_unique_constraints("paper_trades")]
        unique += [i["column_names"] for i in inspector.get_indexes("paper_trades") if i["unique"]]
        assert ["alert_id"] in unique

    def test_the_contract_columns_exist(self):
        cols = set(METADATA.tables["paper_trades"].c.keys())
        wanted = {
            "id", "alert_id", "status", "close_reason", "grade", "instrument_key", "series_key",
            "kind", "side", "direction", "strategy", "timeframe", "confidence", "product", "qty",
            "entry_ts", "fill_bar_ts", "last_bar_ts", "resolution_min", *MONEY_COLUMNS,
            "created_at", "expires_at", "data_source",
        }  # fmt: skip
        assert wanted <= cols, wanted - cols
        assert {"id", "trade_id", "ts", "kind", "detail"} <= set(
            METADATA.tables["paper_events"].c.keys()
        )


class TestConfig:
    def test_defaults_are_enabled_with_a_fifteen_minute_feed_delay(self):
        cfg = PaperCfg()
        assert cfg.enabled is True and cfg.feed_delay_minutes == 15

    def test_the_app_config_carries_a_paper_section(self):
        assert AppConfig().paper == PaperCfg()

    def test_the_shipped_yaml_has_the_key_and_loads(self):
        raw = yaml.safe_load((H.ROOT / "config" / "config.yaml").read_text())
        assert set(raw["paper"]) <= {"enabled", "feed_delay_minutes"}
        cfg = AppConfig.load()
        assert cfg.paper.enabled is True and cfg.paper.feed_delay_minutes == 15

    def test_values_can_be_overridden(self):
        cfg = AppConfig.model_validate({"paper": {"enabled": False, "feed_delay_minutes": 5}})
        assert cfg.paper.enabled is False and cfg.paper.feed_delay_minutes == 5

    @pytest.mark.parametrize(
        "bad", [{"feed_delay": 5}, {"enabled": True, "extra": 1}, {"on": True}]
    )
    def test_a_misspelled_key_is_a_startup_error_not_a_silent_default(self, bad):
        with pytest.raises(ValidationError):
            AppConfig.model_validate({"paper": bad})


class TestPublicContract:
    def test_constructor_takes_the_injected_dependencies_in_contract_order(self):
        sig = inspect.signature(H.paper_book_class().__init__)
        assert list(sig.parameters)[1:] == [
            "engine", "cfg", "bt", "specs", "cost_for", "premium_at", "clock"
        ]  # fmt: skip
        assert sig.parameters["clock"].default is not inspect.Parameter.empty

    def test_the_methods_have_the_contract_signatures(self):
        cls = H.paper_book_class()
        assert list(inspect.signature(cls.on_alert).parameters) == ["self", "alert", "now"]
        step = inspect.signature(cls.step)
        assert list(step.parameters) == ["self", "now", "bars", "strategy_exit"]
        assert step.parameters["strategy_exit"].default is None
        assert list(inspect.signature(cls.open_trades).parameters) == ["self"]
        assert list(inspect.signature(cls.status).parameters) == ["self"]

    def test_the_book_takes_no_risk_governor(self):
        names = set(inspect.signature(H.paper_book_class().__init__).parameters)
        assert not {n for n in names if "governor" in n or "risk" in n}

    def test_a_paper_trade_is_a_frozen_dataclass_mirroring_the_row(self, db):
        trade = H.open_trade(H.make_book(db.engine), alert_id="A1")
        assert is_dataclass(trade) and trade.__dataclass_params__.frozen
        names = {f.name for f in fields(trade)}
        assert {
            "id", "alert_id", "status", "close_reason", "grade", "instrument_key", "series_key",
            "kind", "side", "direction", "strategy", "timeframe", "confidence", "product", "qty",
            "entry_ts", "fill_bar_ts", "entry_paise", "exit_paise", "gross_paise", "costs_paise",
            "net_paise", "risk_paise", "created_at", "expires_at",
        } <= names  # fmt: skip

    def test_a_paper_event_is_a_frozen_dataclass(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        (event,) = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        assert is_dataclass(event) and event.__dataclass_params__.frozen
        assert {"trade_id", "ts", "kind", "detail"} <= {f.name for f in fields(event)}
        assert event.ts.tzinfo is not None


class TestHermetic:
    @staticmethod
    def flow(db):
        """Stock fill, option fill, a gap NO_FILL, a late NO_FILL and a restart reload."""
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="S1")
        H.open_trade(book, alert_id="O1", kind="INDEX")
        H.open_trade(book, alert_id="G1", instrument_key="RELIANCE", stop=1005.0)
        H.open_trade(book, alert_id="E1", instrument_key="HDFCBANK")
        book.step(
            H.at(10, 20),
            H.Feed(
                SBIN=H.standard_bars(),
                BANKNIFTY=H.minute_bars(H.at(10, 5), [54000.0, 54000.0, 54001.0]),
                RELIANCE=H.standard_bars(),
                HDFCBANK=H.minute_bars(H.at(10, 16), [1000.0] * 3),
            ),
        )
        H.make_book(db.engine).open_trades()
        statuses = {r["alert_id"]: H.val(r["status"]) for r in H.trade_rows(db.engine)}
        assert statuses == {"S1": "OPEN", "O1": "OPEN", "G1": "NO_FILL", "E1": "NO_FILL"}

    def test_the_book_never_reads_the_process_wide_settings(self, db, monkeypatch):
        from onelot import settings as process_settings

        def refuse(*_a, **_k):
            raise AssertionError("the paper book must not read current_settings() or YAML")

        process_settings.current_settings.cache_clear()
        monkeypatch.setattr(AppConfig, "load", classmethod(lambda cls, *a, **k: refuse()))
        monkeypatch.setattr(process_settings, "load_yaml", refuse)
        try:
            self.flow(db)
        finally:
            process_settings.current_settings.cache_clear()

    def test_costs_yaml_is_not_parsed_by_the_book(self, db, monkeypatch):
        calls: list[object] = []
        real = CostConfig.load.__func__  # type: ignore[attr-defined]

        def counting(cls, path):
            calls.append(path)
            return real(cls, path)

        monkeypatch.setattr(CostConfig, "load", classmethod(counting))
        self.flow(db)
        assert calls == [], "costs.yaml was re-parsed inside the book (inject cost_for instead)"

    def test_the_paper_package_never_names_the_governor_or_the_process_settings(self):
        package = H.ROOT / "src" / "onelot" / "paper"
        assert (package / "book.py").exists(), "onelot.paper.book is missing"
        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name | ast.Attribute):
                    name = node.id if isinstance(node, ast.Name) else node.attr
                    if "governor" in name.lower() or "current_settings" in name.lower():
                        offenders.append(f"{path.name}: {name}")
                elif isinstance(node, ast.Import | ast.ImportFrom):
                    modules = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or "", *(a.name for a in node.names)]
                    )
                    for module in modules:
                        low = module.lower()
                        if (
                            "governor" in low
                            or "current_settings" in low
                            or low
                            in (
                                "settings",
                                "risk",
                            )
                            or low.startswith(
                                ("risk.", "settings.", "onelot.risk", "onelot.settings")
                            )
                        ):
                            offenders.append(f"{path.name}: import {module}")
        assert not offenders, offenders


class TestThePaperBookCannotReachABroker:
    def test_importing_the_paper_package_loads_no_broker_notifier_or_http_client(self):
        """The book is evidence, never an order: nothing it imports can talk to a broker, send a
        notification or open a socket. Checked in a clean interpreter (not this test process)."""
        import subprocess
        import sys

        code = (
            "import sys; sys.path.insert(0, {!r})\n"
            "import onelot.paper, onelot.paper.factory\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('requests', 'httpx', 'yfinance', 'dhanhq', 'websocket', 'websockets', 'urllib3', "
            "'aiohttp') or m.startswith(('onelot.broker', 'onelot.notify', 'onelot.market', "
            "'onelot.trade', 'onelot.engine', 'onelot.api')))\n"
            "print(bad)\n"
        ).format(str(H.ROOT / "src"))
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "[]", done.stdout


class TestPerformanceProxies:
    def test_bars_is_called_once_per_series_per_step(self, db):
        book = H.make_book(db.engine)
        for i in range(4):
            H.open_trade(book, alert_id=f"S{i}")
        for i in range(3):
            H.open_trade(book, alert_id=f"R{i}", instrument_key="RELIANCE")
        H.open_trade(book, alert_id="O1", kind="INDEX")
        feed = H.Feed(
            SBIN=H.standard_bars(),
            RELIANCE=H.standard_bars(),
            BANKNIFTY=H.minute_bars(H.at(10, 5), [54000.0, 54000.0, 54001.0]),
        )
        book.step(H.at(10, 8), feed)
        assert sorted(feed.calls) == ["BANKNIFTY", "RELIANCE", "SBIN"]
        assert len(H.event_rows(db.engine)) == 8 + 8  # 8 OPENED + 8 FILLED
        again = H.Feed(SBIN=H.standard_bars(), RELIANCE=H.standard_bars(), BANKNIFTY=None)
        book.step(H.at(10, 8), again)
        assert len(again.calls) == len(set(again.calls)), "a series was read twice in one step"

    def test_forty_five_waiting_trades_on_30000_row_frames_fill_inside_the_budget(self, db):
        days = pd.bdate_range(end=pd.Timestamp(H.DAY), periods=80)
        stamps = []
        for day in days:
            stamps += list(
                pd.date_range(
                    day + pd.Timedelta(hours=9, minutes=15),
                    periods=375,
                    freq="min",
                    tz="Asia/Kolkata",
                )
            )
        assert len(stamps) == 30_000
        frame = H.bars_at(stamps, [1000.0] * len(stamps))
        book = H.make_book(db.engine)
        series = ("SBIN", "RELIANCE", "HDFCBANK")
        for i in range(45):
            H.open_trade(book, alert_id=f"P{i}", instrument_key=series[i % 3])
        feed = H.Feed(**dict.fromkeys(series, frame))
        started = time.perf_counter()
        events = book.step(H.at(15, 40), feed)
        elapsed = time.perf_counter() - started
        assert sorted(feed.calls) == sorted(series)  # once per series
        assert len(events) == 45 and all(H.val(e.kind) == "FILLED" for e in events)
        assert {H.val(r["status"]) for r in H.trade_rows(db.engine)} == {"OPEN"}
        assert elapsed < 2.0, f"45 fills on 30,000-row frames took {elapsed:.2f}s (budget 0.2s)"
