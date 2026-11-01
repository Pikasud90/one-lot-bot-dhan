"""scripts/gate.sh and the CONTRIBUTING.md that documents it (US-000 AC4, AC5).

The doc test keeps the written instructions and the gate the same list of commands, so a
contributor who follows CONTRIBUTING.md runs exactly what CI runs. The gate tests run a
copy of gate.sh against a fake interpreter that logs each step and fails on demand: no
real ruff, mypy or pytest runs (which would also recurse into this suite).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "gate.sh"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
VENV_PYTHON = ".venv/bin/python"
# The interpreter's arguments for each step, spelled as in gate.sh (shell quoting included).
GATE_STEPS = [
    "-m ruff format --check .",
    "-m ruff check .",
    "scripts/mypy_ratchet.py",
    '-m pytest -m "not live"',
]
# The same steps as the fake interpreter logs them: "$*" after the shell removed quotes.
LOGGED_STEPS = [" ".join(shlex.split(step)) for step in GATE_STEPS]

FAKE_PYTHON = """\
#!/usr/bin/env bash
# Logs its arguments; exits 1 when they contain $FAKE_FAIL_ON.
echo "$0 $*" >> "$FAKE_LOG"
if [[ -n "${FAKE_FAIL_ON:-}" && "$*" == *"$FAKE_FAIL_ON"* ]]; then
  exit 1
fi
exit 0
"""


def gate_commands() -> list[str]:
    """Commands gate.sh runs, with the interpreter variable spelled as the venv path."""
    commands = []
    for line in GATE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith('"$PY" '):
            commands.append(VENV_PYTHON + stripped.removeprefix('"$PY"'))
    return commands


def running_everything_section() -> str:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    match = re.search(r"^## Running everything\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, "CONTRIBUTING.md has no '## Running everything' section"
    return match.group("body")


def ruff_pin() -> str:
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    match = re.search(r"^ruff==\S+", text, re.M)
    assert match, "requirements-dev.txt does not pin ruff exactly"
    return match.group(0)


# --------------------------------------------------------------------------- AC5 doc test
def test_gate_runs_the_four_contract_steps_in_order() -> None:
    assert gate_commands() == [f"{VENV_PYTHON} {step}" for step in GATE_STEPS]


def test_contributing_lists_exactly_the_gate_commands_in_one_bash_block() -> None:
    blocks = re.findall(r"^```bash\n(.*?)^```", running_everything_section(), re.M | re.S)
    assert len(blocks) == 1, f"expected one bash block, found {len(blocks)}"
    listed = [
        line.strip()
        for line in blocks[0].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert listed == gate_commands()


def test_contributing_states_the_ruff_format_policy_with_the_pinned_version() -> None:
    section = running_everything_section()
    assert "scripts/gate.sh" in section
    assert re.search(r"format policy", section, re.I), "no ruff format policy sentence"
    assert ruff_pin() in section, "the policy must name the same ruff pin as requirements-dev"


# --------------------------------------------------------------------------- AC4 gate.sh
def test_gate_is_an_executable_strict_bash_script() -> None:
    text = GATE.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    assert "\r" not in text, "CRLF breaks bash"
    if os.name != "nt":
        assert GATE.stat().st_mode & stat.S_IXUSR, "chmod +x scripts/gate.sh"


needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None, reason="needs a POSIX bash"
)


def _gate_copy(tmp_path: Path, *, venv: bool) -> tuple[Path, Path]:
    """A scratch repo holding gate.sh and a fake interpreter; returns (root, fake)."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(GATE, root / "scripts" / "gate.sh")
    fake_dir = root / ".venv" / "bin" if venv else tmp_path / "bin"
    fake_dir.mkdir(parents=True)
    fake = fake_dir / "python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(0o755)
    return root, fake


def _run_gate(
    root: Path, tmp_path: Path, fail_on: str = "", path_prefix: str = "", path: str | None = None
):
    """Run gate.sh from outside the repo; ``path`` replaces PATH, ``path_prefix`` extends it."""
    log = tmp_path / "steps.log"
    env = dict(os.environ, FAKE_LOG=str(log), FAKE_FAIL_ON=fail_on)
    if path is not None:
        env["PATH"] = path
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env["PATH"]
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "gate.sh")],
        cwd=tmp_path,  # not the repo: the gate must find its own root
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    steps = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, steps


@needs_bash
def test_gate_runs_every_step_with_the_venv_python_from_the_repo_root(tmp_path: Path) -> None:
    root, fake = _gate_copy(tmp_path, venv=True)
    proc, steps = _run_gate(root, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert steps == [f"{fake} {step}" for step in LOGGED_STEPS]


@needs_bash
@pytest.mark.parametrize("failing", range(len(LOGGED_STEPS)), ids=LOGGED_STEPS)
def test_gate_stops_with_a_non_zero_exit_at_the_first_failing_step(
    tmp_path: Path, failing: int
) -> None:
    root, _ = _gate_copy(tmp_path, venv=True)
    proc, steps = _run_gate(root, tmp_path, fail_on=LOGGED_STEPS[failing])
    assert proc.returncode != 0
    assert len(steps) == failing + 1, f"steps after the failure still ran: {steps}"


@needs_bash
def test_gate_falls_back_to_python_on_path_without_a_venv(tmp_path: Path) -> None:
    root, fake = _gate_copy(tmp_path, venv=False)
    proc, steps = _run_gate(root, tmp_path, path_prefix=str(fake.parent))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [s.split(" ", 1)[1] for s in steps] == LOGGED_STEPS


@needs_bash
def test_gate_falls_back_to_python3_without_a_venv_or_a_bare_python(tmp_path: Path) -> None:
    """Stock macOS and Debian ship only ``python3``; the gate must still find it."""
    root, fake = _gate_copy(tmp_path, venv=False)
    python3 = fake.rename(fake.with_name("python3"))
    tools = tmp_path / "tools"  # just what gate.sh and the fake need: no real python on PATH
    tools.mkdir()
    for tool in ("bash", "dirname"):
        found = shutil.which(tool)
        assert found, f"{tool} is not on PATH"
        (tools / tool).symlink_to(found)
    proc, steps = _run_gate(root, tmp_path, path=f"{python3.parent}{os.pathsep}{tools}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert steps == [f"{python3} {step}" for step in LOGGED_STEPS]
