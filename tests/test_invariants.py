"""Safety invariants, enforced by scanning the source tree.

These are the properties the whole design rests on. They are checked
mechanically because a convention that is only written down is a convention
that eventually gets broken by a well-meaning edit.
"""

from __future__ import annotations

import ast
import io
import logging
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "onelot"


def python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestNoOrderPlacement:
    """*** INVARIANT I1 ***

    The system must contain no code path that can place, modify or cancel an
    order. This is the single property that makes it safe to run unattended.
    """

    # Dhan/broker write APIs and their common aliases.
    FORBIDDEN_CALLS = [
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

    def test_no_forbidden_call_appears_in_source(self):
        offenders: list[str] = []
        for f in python_files():
            text = f.read_text(encoding="utf-8")
            # Strip comments and docstrings so prose mentioning these names
            # (this file's own siblings do) cannot trip the check.
            try:
                tree = ast.parse(text)
            except SyntaxError:  # pragma: no cover
                continue
            code_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    code_names.add(node.attr.lower())
                elif isinstance(node, ast.Name):
                    code_names.add(node.id.lower())
            for bad in self.FORBIDDEN_CALLS:
                if bad in code_names:
                    offenders.append(f"{f.relative_to(SRC)}: {bad}")
        assert not offenders, (
            "ORDER-PLACEMENT CODE DETECTED — this system must never be able to "
            "trade:\n  " + "\n  ".join(offenders)
        )

    def test_broker_protocol_exposes_no_write_method(self):
        from onelot.broker.base import BrokerAdapter

        methods = {m for m in dir(BrokerAdapter) if not m.startswith("_")}
        for bad in self.FORBIDDEN_CALLS:
            assert not any(bad in m.lower() for m in methods), f"BrokerAdapter exposes {bad}"

    def test_dhan_adapter_exposes_no_write_method(self):
        from onelot.broker.dhan import DhanAdapter

        methods = {m for m in dir(DhanAdapter) if not m.startswith("_")}
        for bad in self.FORBIDDEN_CALLS:
            assert not any(bad in m.lower() for m in methods), f"DhanAdapter exposes {bad}"

    def test_no_post_to_an_order_endpoint(self):
        """Even hand-rolled HTTP must not reach an orders endpoint."""
        pattern = re.compile(r"[\"'](/v2)?/orders?\b", re.I)
        offenders = [
            str(f.relative_to(SRC))
            for f in python_files()
            if pattern.search(f.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"order endpoint referenced in: {offenders}"


class TestSecretSafety:
    """*** INVARIANT I6 *** — secrets never reach logs."""

    def test_a_known_token_never_appears_in_log_output(self):
        from onelot.logging_setup import get_redactor, setup_logging

        fake = "eyJhbGciOiJIUzI1NiJ9.eyJkaGFuQ2xpZW50SWQiOiIxMTAwMTIzNDU2In0.QQQQQQQQQQQQQQQQQQQQ"
        buf = io.StringIO()
        setup_logging(level="INFO", secrets=[fake])
        h = logging.StreamHandler(buf)
        h.addFilter(get_redactor())
        logging.getLogger().addHandler(h)
        log = logging.getLogger("invariant-test")
        try:
            log.info("connecting access_token=%s", fake)
            log.info("bare token %s", fake)
            try:
                raise RuntimeError(f"HTTP 401 for {fake}")
            except RuntimeError:
                log.exception("request failed")
        finally:
            logging.getLogger().removeHandler(h)
        assert fake not in buf.getvalue()
        assert "REDACTED" in buf.getvalue()

    def test_secrets_repr_does_not_leak(self):
        from onelot.config import Secrets

        s = Secrets("CLIENT123456", "supersecrettoken12345", "apitoken12345")
        assert "supersecrettoken" not in repr(s)
        assert "supersecrettoken" not in str(s)

    def test_no_hardcoded_credentials_in_source(self):
        jwt = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.")
        offenders = []
        for f in python_files():
            if f.name in ("logging_setup.py",):  # contains the detector itself
                continue
            if jwt.search(f.read_text(encoding="utf-8")):
                offenders.append(str(f.relative_to(SRC)))
        assert not offenders, f"JWT-shaped literal in: {offenders}"


class TestDefaults:
    def test_paper_is_the_default_mode(self, cfg):
        """LIVE must be a deliberate edit, never the shipped state."""
        assert cfg.mode == "PAPER"

    def test_capital_is_optional_and_never_blocks(self, cfg):
        """With one fixed lot there is nothing to size, so an undeclared
        account size cannot make the engine unsafe. Capital 0 means 'not
        disclosed', not 'blocked'."""
        assert cfg.blocking_problems() == []

    def test_one_lot_is_the_shipped_rule(self, cfg):
        assert cfg.risk.lots_per_trade == 1

    def test_risk_limits_are_absolute_rupees(self, cfg):
        """Rupee limits mean the engine never needs the account size."""
        assert cfg.risk.daily_loss_limit_inr > 0
        assert cfg.risk.max_drawdown_inr >= cfg.risk.daily_loss_limit_inr

    def test_regime_defaults_to_the_simple_engine(self, cfg):
        """The heavy model is opt-in. Simpler is the default."""
        assert cfg.strategy.regime.engine == "rule_based"

    def test_api_binds_to_loopback(self, cfg):
        assert cfg.api.host == "127.0.0.1"
        assert cfg.api.allow_non_loopback is False

    def test_non_loopback_bind_requires_explicit_opt_in(self):
        from onelot.config import ApiCfg

        with pytest.raises(ValueError, match="loopback"):
            ApiCfg(host="0.0.0.0")
        ApiCfg(host="0.0.0.0", allow_non_loopback=True)  # explicit is allowed

    def test_chain_poll_cannot_breach_the_broker_limit(self):
        from onelot.config import DataCfg

        with pytest.raises(ValueError, match="RL001"):
            DataCfg(chain_poll_interval_sec=2.0)

    def test_costs_ship_unverified(self, cost_cfg):
        assert cost_cfg.verified_against_contract_note is False


class TestNoLookAheadInStorage:
    """*** INVARIANT I2 *** at the storage boundary."""

    def test_get_candles_excludes_forming_bars_by_default(self):
        import inspect

        from onelot.store.repo import Repo

        sig = inspect.signature(Repo.get_candles)
        assert sig.parameters["closed_only"].default is True


class TestLogFormatStrings:
    """Every %-style logging call must actually format.

    Regression: `log.info("... Rs %+,.0f", net)` raises ValueError, because
    printf-style formatting has no comma-grouping flag. `logging` swallows
    that into a "Logging error" on stderr, so the line silently vanished on
    EVERY position close and nothing failed loudly. Use an f-string for
    grouped numbers.
    """

    # Matches a %-conversion whose flags include a comma, e.g. %+,.0f or %,.2f
    BAD_GROUPING = re.compile(r"%[#0\- +]*,[^%]*?[diouxXeEfFgGcrsa]")

    def test_no_comma_grouping_in_percent_format_strings(self):
        offenders: list[str] = []
        for f in python_files():
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "log." not in line and "logger." not in line:
                    continue
                if self.BAD_GROUPING.search(line):
                    offenders.append(f"{f.relative_to(SRC)}:{i}: {stripped[:90]}")
        assert not offenders, (
            "printf-style formatting has no comma-grouping flag; these log "
            "calls raise ValueError at runtime:\n  " + "\n  ".join(offenders)
        )

    def test_the_specific_bad_format_really_does_raise(self):
        """Guards the guard: if this ever stops raising, the check above is moot."""
        with pytest.raises(ValueError):
            _ = "Rs %+,.0f" % 1234.5  # noqa: F509  (deliberately invalid)
        assert f"Rs {1234.5:+,.0f}" == "Rs +1,234"

    def test_percent_format_calls_are_arity_correct(self):
        """A %-count that disagrees with the argument count is the other way
        these calls fail silently."""
        conv = re.compile(r"%[#0\- +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]")
        offenders: list[str] = []
        for f in python_files():
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in (
                    "debug",
                    "info",
                    "warning",
                    "error",
                    "exception",
                    "critical",
                ):
                    continue
                # Only real loggers. `notifier.warning(title, body)` shares the
                # method name but is not %-formatted, and matching it would
                # make this check cry wolf.
                recv = node.func.value
                recv_name = (
                    recv.id
                    if isinstance(recv, ast.Name)
                    else recv.attr
                    if isinstance(recv, ast.Attribute)
                    else ""
                )
                if recv_name not in ("log", "logger", "logging", "LOG", "_log"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                fmt = node.args[0].value
                if not isinstance(fmt, str):
                    continue
                n_conv = len(conv.findall(fmt.replace("%%", "")))
                n_args = len(node.args) - 1
                if n_conv != n_args:
                    offenders.append(
                        f"{f.relative_to(SRC)}:{node.lineno}: {n_conv} placeholders "
                        f"but {n_args} args -- {fmt[:60]!r}"
                    )
        assert not offenders, "malformed logging calls:\n  " + "\n  ".join(offenders)
