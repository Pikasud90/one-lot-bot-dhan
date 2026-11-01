"""US-002 non-functional requirements and the public contract.

* the contract of `onelot.paper.manage` (CloseReason, StrategyExit, PaperClose, PositionState,
  ManageRules, manage_step) and of `PaperBook.step(now, bars, strategy_exit)`;
* deterministic and pure: manage_step performs no I/O, reads no clock, draws no random number;
  prices are integer paise;
* enum member names must not be SQUARE_OFF (the invariant scanner) and no module of this story
  uses an order-verb identifier;
* the events manage_step returns are `PaperEvent`s of the book (kind CLOSED for the close).

Extra fields beyond the story's contract are tolerated only when they have defaults, so the pair
can add a cursor or a trade id without breaking callers that build the contract's states.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import typing
from pathlib import Path

import pytest
import us001_helpers as H
import us002_helpers as U

SRC = Path(H.ROOT) / "src" / "onelot"
NEW_MODULES = [SRC / "desk" / "rules.py", SRC / "paper" / "manage.py"]
CLOSE_REASONS = {
    "STOP",
    "TRAIL_STOP",
    "TARGET",
    "SIGNAL_EXIT",
    "OPPOSITE_SIGNAL",
    "SESSION_END",
    "TIME_STOP",
    "NO_FILL",
    "GAP_THROUGH_STOP",
}
# the order-verb list of tests/test_invariants.py (INVARIANT I1), kept literally in sync
FORBIDDEN = [
    "place_order",
    "placeorder",
    "modify_order",
    "modifyorder",
    "cancel_order",
    "cancelorder",
    "place_slice_order",
    "placesliceorder",
    "convert_position",
    "convertposition",
    "exit_position",
    "exitposition",
    "square_off",
    "squareoff_position",
    "place_forever",
    "edit_forever",
    "cancel_forever",
    "kill_switch_order",
]
NO_IO_MODULES = {
    "socket",
    "sqlite3",
    "requests",
    "httpx",
    "urllib",
    "urllib3",
    "sqlalchemy",
    "subprocess",
    "yfinance",
    "aiohttp",
    "websocket",
    "websockets",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def fields(cls) -> dict[str, dataclasses.Field]:
    return {f.name: f for f in dataclasses.fields(cls)}


def required(cls) -> set[str]:
    return {
        n
        for n, f in fields(cls).items()
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }


class TestThePublicContract:
    def test_close_reason_is_the_nine_story_values_and_never_square_off(self):
        """The nine of the story; Manager decision 3 (gap_through_target) may add one tenth."""
        values = set(typing.get_args(U.manage().CloseReason))
        assert CLOSE_REASONS <= values <= CLOSE_REASONS | {"GAP_THROUGH_TARGET"}
        assert "SQUARE_OFF" not in values

    def test_strategy_exit_is_a_frozen_value_of_reason_and_bar_close_ts(self):
        cls = U.manage().StrategyExit
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen
        assert {"reason", "bar_close_ts"} <= set(fields(cls))
        assert required(cls) == {"reason", "bar_close_ts"}
        exit_ = cls("SIGNAL_EXIT", H.at(10, 10))
        with pytest.raises(dataclasses.FrozenInstanceError):
            exit_.reason = "OPPOSITE_SIGNAL"

    def test_paper_close_is_a_frozen_value_of_reason_price_at_gap(self):
        cls = U.manage().PaperClose
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen
        assert {"reason", "price_paise", "at", "gap"} <= set(fields(cls))
        close = cls(reason="STOP", price_paise=99000, at=H.at(10, 6), gap=False)
        assert (close.reason, close.price_paise, close.gap) == ("STOP", 99000, False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            close.price_paise = 1

    def test_position_state_is_a_mutable_value_with_the_contract_fields(self):
        cls = U.manage().PositionState
        assert dataclasses.is_dataclass(cls) and not cls.__dataclass_params__.frozen
        contract = {
            "direction",
            "kind",
            "qty",
            "entry_paise",
            "initial_stop_paise",
            "stop_paise",
            "target_paise",
            "trail_active",
            "opened_at",
            "last_bar_ts",
            "bars_held",
            "sessions_held",
            "option",
        }
        assert contract <= set(fields(cls))
        extras = set(fields(cls)) - contract
        assert required(cls) <= contract | {"trade_id"}, f"extras must default: {extras}"

    def test_manage_rules_is_a_frozen_value_with_the_contract_fields(self):
        cls = U.manage().ManageRules
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen
        contract = {
            "target_r",
            "trail_atr_mult",
            "trail_activate_r",
            "atr",
            "max_bars",
            "tf_min",
            "exit_time",
            "tick_paise",
        }
        assert contract <= set(fields(cls))
        assert required(cls) <= contract, "an extra rule must have a default"
        rules = U.mk_rules()
        with pytest.raises(dataclasses.FrozenInstanceError):
            rules.max_bars = 9

    def test_manage_step_has_the_contract_signature(self):
        sig = inspect.signature(U.manage().manage_step)
        assert list(sig.parameters)[:5] == ["state", "bars", "rules", "now", "strategy_exit"]
        assert sig.parameters["strategy_exit"].default is None

    def test_the_book_step_signature_takes_the_strategy_exit_callback(self):
        sig = inspect.signature(H.paper_book_class().step)
        assert list(sig.parameters)[1:4] == ["now", "bars", "strategy_exit"]
        assert sig.parameters["strategy_exit"].default is None

    def test_manage_step_returns_a_state_and_a_list_of_the_books_paper_events(self):
        from onelot.paper.book import PaperEvent

        state, events = U.run(U.mk_state(), U.pbars(H.at(10, 6), [(100000, 100100, 99000, 99500)]))
        assert isinstance(events, list) and isinstance(state, U.manage().PositionState)
        assert events and all(isinstance(e, PaperEvent) for e in events)
        (closed,) = U.closes_of(events)
        assert closed.ts.tzinfo is not None and isinstance(closed.detail["price_paise"], int)

    def test_every_price_the_close_carries_is_a_plain_int_of_paise(self):
        state, events = U.run(U.mk_state(), U.pbars(H.at(10, 6), [(100000, 100100, 99000, 99500)]))
        close = U.close_of(events)
        assert type(close.price) is int and type(state.stop_paise) is int
        assert isinstance(close.gap, bool)

    def test_a_naive_now_is_rejected_not_guessed(self):
        bars = U.pbars(H.at(10, 6), [U.flat()[0]])
        with pytest.raises((ValueError, TypeError)):
            U.manage().manage_step(
                U.mk_state(), bars, U.mk_rules(), dt.datetime(2026, 10, 29, 10, 7)
            )

    def test_a_naive_bar_index_is_rejected_not_guessed(self):
        bars = U.pbars(H.at(10, 6), [U.flat()[0]]).tz_localize(None)
        with pytest.raises((ValueError, TypeError)):
            U.run(U.mk_state(), bars, now=H.at(10, 7))


class TestPureAndSafe:
    @pytest.mark.parametrize("path", NEW_MODULES, ids=lambda p: p.name)
    def test_no_io_import_no_clock_no_randomness_no_print(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]} if node.level == 0 else set()
            else:
                continue
            assert not roots & NO_IO_MODULES, f"{path.name} imports {roots & NO_IO_MODULES}"
            assert "random" not in roots
        called = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert not called & {"now", "utcnow", "today", "now_ist", "open", "print", "input"}, called

    @pytest.mark.parametrize("path", NEW_MODULES, ids=lambda p: p.name)
    def test_no_order_verb_identifier_and_no_square_off_member(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.arg)):
                names.add((getattr(node, "name", None) or getattr(node, "arg", "")).lower())
        assert not names & set(FORBIDDEN), names & set(FORBIDDEN)
        literals = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert "SQUARE_OFF" not in literals, "the invariant scanner bans that member name"

    def test_the_rules_module_never_imports_the_paper_package(self):
        """The desk adopts rules.py in US-025: it must not drag the paper package with it."""
        tree = ast.parse((SRC / "desk" / "rules.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "paper" not in (node.module or "").split("."), node.module
            elif isinstance(node, ast.Import):
                assert not any("paper" in a.name.split(".") for a in node.names)

    def test_importing_the_paper_package_with_manage_loads_no_broker_notifier_or_http_client(self):
        """The US-001 NFR, kept true once `manage` (which uses `onelot.desk.rules`) is part of the
        paper package: the book is evidence, so nothing it imports may reach a broker, notify or
        open a socket. NOTE: `onelot.desk/__init__.py` imports the whole Desk, so a plain
        `from ..desk.rules import ...` in the paper package drags the notifier in; the pair has to
        keep the desk package import out of this path (a lazy `Desk` in the package, or the rules
        reached without the package `__init__`)."""
        import subprocess
        import sys

        code = (
            "import sys; sys.path.insert(0, {!r})\n"
            "import onelot.paper, onelot.paper.factory, onelot.paper.manage\n"
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

    def test_manage_step_is_deterministic_across_repeated_calls(self):
        bars = U.pbars(
            H.at(10, 6), [(100000, 101200, 100100, 101100), (101100, 101300, 100500, 101250)]
        )
        results = [U.run(U.mk_state(), bars, U.trail_rules()) for _ in range(3)]
        assert all(
            U.canon(r[1]) == U.canon(results[0][1]) and r[0] == results[0][0] for r in results
        )
