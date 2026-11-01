"""US-000 AC4: ``scripts/gate.sh`` is the one repo-level command behind the quality gate.

Static checks cover the script text and its git mode.  Functional checks run the real script in a
sandbox whose tools (python, ruff, pytest, mypy) are stubs that log their invocation and fail on
demand, so "exits non-zero if any step fails" is proven without running the real tools.  The
skill's own ``quality_gate.py`` verdict (also part of AC4) lives outside the repo; the Manager
runs it at gate G2.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "gate.sh"
PYPROJECT = ROOT / "pyproject.toml"
STEPS = ("format", "check", "ratchet", "pytest")


def _gate_text() -> str:
    assert GATE.is_file(), "scripts/gate.sh does not exist yet (US-000 AC4)"
    return GATE.read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    """The script's non-blank, non-comment lines."""
    return [
        line.strip()
        for line in _gate_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# --------------------------------------------------------------------------------------------
# static: file, mode, content
# --------------------------------------------------------------------------------------------
def test_gate_script_exists_and_starts_with_a_shell_shebang():
    first = _gate_text().splitlines()[0]
    assert re.match(r"#!\s*(/usr/bin/env\s+)?(/bin/)?(ba)?sh\b", first), f"bad shebang: {first!r}"


def test_gate_script_is_executable_on_disk():
    assert GATE.is_file(), "scripts/gate.sh does not exist yet (US-000 AC4)"
    assert os.access(GATE, os.X_OK), "scripts/gate.sh is not executable (chmod +x)"


def test_gate_script_is_committed_with_the_executable_bit():
    done = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts/gate.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.stdout.strip(), "scripts/gate.sh is not tracked by git (git add it)"
    mode = done.stdout.split()[0]
    assert mode == "100755", f"git records mode {mode}; a fresh clone would lose +x"


def test_gate_script_contains_the_four_steps():
    lines = _code_lines()
    assert any(re.search(r"ruff\s+format\s+--check\s+\.(\s|$)", ln) for ln in lines), (
        "no `ruff format --check .` step"
    )
    assert any(re.search(r"ruff\s+check\b", ln) for ln in lines), "no `ruff check` step"
    assert any("mypy_ratchet.py" in ln for ln in lines), "no mypy ratchet step"
    assert any(re.search(r"(?<![\w.-])pytest(?![\w.-])", ln) for ln in lines), "no pytest step"


def test_gate_script_fails_when_any_step_fails_by_construction():
    text = "\n".join(_code_lines())
    errexit = re.search(r"^set\s+(-[A-Za-z]*e[A-Za-z]*|-o\s+errexit)", text, re.MULTILINE)
    explicit = text.count("|| exit") >= 4 or text.count("&&") >= 3
    assert errexit or explicit, "script neither uses `set -e` (errexit) nor guards every step"
    for masking in (r"\|\|\s*true\b", r"\|\|\s*:", r"\bset\s+\+e\b", r";\s*true\b"):
        assert not re.search(masking, text), f"script masks a failure with {masking!r}"
    for line in _code_lines():
        piped = re.search(r"(?<!\|)\|(?!\|)", line)
        if piped and "pytest" in line:
            assert "pipefail" in text, f"pytest is piped without pipefail: {line}"


def test_gate_script_does_not_depend_on_the_claude_skill_or_one_machine():
    text = _gate_text()
    for forbidden in (".claude", "agile-ai-finance-team", "/Users/"):
        assert forbidden not in text, f"scripts/gate.sh must not mention {forbidden!r}"


def test_gate_pytest_step_cannot_drop_the_live_test_deselection():
    text = _gate_text()
    assert "addopts" not in text, "gate.sh must not override pytest addopts"
    for line in _code_lines():
        if re.search(r"(?<![\w.-])pytest(?![\w.-])", line) and re.search(r"\s-m\s", line):
            assert "not live" in line, f"pytest -m expression drops `not live`: {line}"


# --------------------------------------------------------------------------------------------
# pyproject: pytest addopts
# --------------------------------------------------------------------------------------------
def _addopts_tokens() -> list[str]:
    options = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    addopts = options.get("addopts", "")
    return shlex.split(addopts) if isinstance(addopts, str) else [str(a) for a in addopts]


def test_pytest_addopts_deselect_live_tests_by_default():
    tokens = _addopts_tokens()
    expressions = [tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token == "-m"] + [
        token[2:] for token in tokens if token.startswith("-m") and len(token) > 2
    ]
    assert any("not live" in expr for expr in expressions), f"addopts lacks -m 'not live': {tokens}"


def test_a_live_marked_test_is_deselected_by_the_repo_pytest_configuration():
    with tempfile.TemporaryDirectory(prefix="us000_live_") as tmp:
        sandbox = Path(tmp)
        shutil.copy2(PYPROJECT, sandbox / "pyproject.toml")
        (sandbox / "src").mkdir()
        tests = sandbox / "tests"
        tests.mkdir()
        (tests / "test_offline.py").write_text("def test_offline():\n    assert True\n", "utf-8")
        (tests / "test_broker_live.py").write_text(
            "import pytest\n\n\n@pytest.mark.live\ndef test_needs_real_credentials():\n"
            "    raise AssertionError('a live test ran in the default run')\n",
            "utf-8",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "PYTHON"))}
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout and "1 deselected" in done.stdout, done.stdout


# --------------------------------------------------------------------------------------------
# functional: run the real script against stub tools
# --------------------------------------------------------------------------------------------
STUB = """#!/bin/sh
# Test double: log the invocation, fail when this call belongs to the step named in GATE_FAIL_STEP.
line="$(basename "$0") $*"
echo "$line | cwd=$(pwd)" >> "$GATE_LOG"
case "$line" in
  *mypy_ratchet*) step=ratchet ;;
  *pytest*) step=pytest ;;
  *format*) step=format ;;
  *check*) step=check ;;
  *) step=other ;;
esac
if [ "$step" = "${GATE_FAIL_STEP:-}" ]; then
  echo "stub: simulated failure of step $step" >&2
  exit 1
fi
exit 0
"""

ACTIVATE = """# stub virtualenv activation
VIRTUAL_ENV="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export VIRTUAL_ENV
export PATH="$VIRTUAL_ENV/bin:$PATH"
"""


class GateSandbox:
    def __init__(self, base: Path) -> None:
        self.root = base / "ONE LOT BOT - DHAN"
        self.log = base / "calls.log"
        (self.root / "tests").mkdir(parents=True)
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        shutil.copy2(GATE, self.root / "scripts" / "gate.sh")
        shutil.copy2(PYPROJECT, self.root / "pyproject.toml")
        stub_dirs = [self.root / ".venv" / "bin", base / "bin"]
        for directory in stub_dirs:
            directory.mkdir(parents=True)
            for tool in ("python", "python3", "python3.12", "ruff", "pytest", "mypy"):
                path = directory / tool
                path.write_text(STUB, encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (self.root / ".venv" / "bin" / "activate").write_text(ACTIVATE, encoding="utf-8")
        self.stub_path = base / "bin"

    def run(self, fail_step: str | None = None) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        assert bash, "bash is required to run scripts/gate.sh"
        env = {
            "PATH": f"{self.stub_path}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.root.parent),
            "GATE_LOG": str(self.log),
            "LC_ALL": "C",
        }
        if fail_step:
            env["GATE_FAIL_STEP"] = fail_step
        return subprocess.run(
            [bash, "scripts/gate.sh"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )

    def steps_run(self) -> list[str]:
        """Recognised steps in the order they ran, e.g. ['format', 'check', 'ratchet', 'pytest']."""
        found = []
        for row in self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []:
            call = row.split(" | cwd=")[0]
            if "mypy_ratchet" in call:
                found.append("ratchet")
            elif re.search(r"(?<![\w.-])pytest(?![\w.-])", call):
                found.append("pytest")
            elif "format" in call:
                found.append("format")
            elif "check" in call:
                found.append("check")
        return found

    def cwds(self) -> set[Path]:
        rows = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return {Path(row.rsplit("cwd=", 1)[1]).resolve() for row in rows}


@contextmanager
def new_gate_sandbox() -> Iterator[GateSandbox]:
    assert GATE.is_file(), "scripts/gate.sh does not exist yet (US-000 AC4)"
    # Outside pytest's tmp tree: that path contains "pytest" and would confuse the stub's matching.
    with tempfile.TemporaryDirectory(prefix="us000_gate_") as tmp:
        yield GateSandbox(Path(tmp))


def test_gate_runs_format_check_ratchet_and_pytest_in_that_order_and_exits_0():
    with new_gate_sandbox() as gate:
        done = gate.run()
        assert done.returncode == 0, done.stdout + done.stderr
        assert gate.steps_run() == list(STEPS), gate.log.read_text(encoding="utf-8")


def test_gate_steps_run_from_the_repo_root():
    with new_gate_sandbox() as gate:
        done = gate.run()
        assert done.returncode == 0, done.stdout + done.stderr
        assert gate.cwds() == {gate.root.resolve()}


@pytest.mark.parametrize("failing_step", STEPS)
def test_gate_exits_non_zero_when_a_single_step_fails(failing_step):
    with new_gate_sandbox() as gate:
        done = gate.run(fail_step=failing_step)
        assert done.returncode != 0, f"gate passed although `{failing_step}` failed\n{done.stdout}"
        assert failing_step in gate.steps_run(), "the failing step never ran"
