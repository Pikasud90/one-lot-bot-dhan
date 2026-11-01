"""US-000 AC2: pinned tooling, ruff scope, formatted tree, and no accidental mypy config.

The behaviour-neutrality of the one-off reformat (identical syntax trees, same passed count,
single commit) is a property of a *change*, not of the tree, so it is verified by the Tester in
mode B against the merge base rather than as a standing test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
PYPROJECT = ROOT / "pyproject.toml"
RUFF_PIN = "0.17.3"


def _requirements() -> list[str]:
    lines = []
    for raw in REQUIREMENTS_DEV.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def _name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, f"cannot read a package name from {requirement!r}"
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _ruff(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# --------------------------------------------------------------------------------------------
# requirements-dev.txt
# --------------------------------------------------------------------------------------------
def test_requirements_dev_pins_ruff_exactly():
    ruff = [line for line in _requirements() if _name(line) == "ruff"]
    assert len(ruff) == 1, f"expected exactly one ruff requirement, found {ruff}"
    assert re.fullmatch(rf"ruff\s*==\s*{re.escape(RUFF_PIN)}", ruff[0]), ruff[0]


def test_requirements_dev_has_no_open_ended_ruff_or_mypy_ranges():
    text = "\n".join(_requirements())
    assert "ruff>=" not in text.replace(" ", "")
    for line in _requirements():
        if _name(line) in {"ruff", "mypy"}:
            assert "==" in line and not re.search(r"[<>~!]=?", line), f"not an exact pin: {line}"


def test_requirements_dev_pins_exactly_one_exact_mypy_version():
    mypy = [line for line in _requirements() if _name(line) == "mypy"]
    assert len(mypy) == 1, f"expected exactly one mypy requirement, found {mypy}"
    assert re.fullmatch(r"mypy==\d+\.\d+\.\d+", mypy[0]), f"not an exact x.y.z pin: {mypy[0]}"


def test_the_venv_ruff_is_the_pinned_version():
    done = _ruff("--version")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"ruff {RUFF_PIN}", done.stdout


# --------------------------------------------------------------------------------------------
# pyproject.toml
# --------------------------------------------------------------------------------------------
def _normalise_exclude(entry: str) -> str:
    entry = entry.strip().removeprefix("./").removeprefix("**/")
    return entry.removesuffix("/**").rstrip("/")


def test_ruff_extend_exclude_lists_agile_docs_and_egg_info():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    ruff = data.get("tool", {}).get("ruff", {})
    assert "extend-exclude" in ruff, "[tool.ruff] has no extend-exclude"
    entries = {_normalise_exclude(entry) for entry in ruff["extend-exclude"]}
    for required in (".agile", "docs", "*.egg-info"):
        assert required in entries, f"extend-exclude misses {required}: {sorted(entries)}"


def test_extend_exclude_keeps_ruff_format_out_of_agile_docs_and_egg_info(tmp_path):
    # Real behaviour with the repo's own ruff config: ruff 0.17 also formats ```python fences in
    # Markdown, so prose directories must be excluded.  src/ is the control (must be flagged).
    shutil.copy2(PYPROJECT, tmp_path / "pyproject.toml")
    ugly_code = 'x = {  "a":1 }\n'
    ugly_prose = "# Notes\n\n```python\nx = {  'a':1 }\n```\n"
    files = {
        "src/pkg/control.py": ugly_code,
        ".agile/stories/US-999.md": ugly_prose,
        ".agile/helper.py": ugly_code,
        "docs/guide.md": ugly_prose,
        "docs/example.py": ugly_code,
        "onelot_desk.egg-info/PKG-INFO.md": ugly_prose,
        "onelot_desk.egg-info/stale.py": ugly_code,
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    done = _ruff("format", "--check", "--output-format", "concise", ".", cwd=tmp_path)
    flagged = {
        match.group(1).replace("\\", "/")
        for line in done.stdout.splitlines()
        if (match := re.match(r"(.+?):\d+:\d+: unformatted", line))
    }
    assert "src/pkg/control.py" in flagged, f"control file not flagged (test is blind): {done}"
    assert flagged == {"src/pkg/control.py"}, f"ruff format still touches prose dirs: {flagged}"


def test_no_mypy_configuration_exists_anywhere_in_the_repo():
    # Any of these would make the skill's quality gate treat mypy as a *required* check.
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "[tool.mypy" not in text, "pyproject.toml configures mypy"
    assert "mypy" not in tomllib.loads(text).get("tool", {})
    forbidden = {"mypy.ini", ".mypy.ini", "setup.cfg"}
    present = [name for name in forbidden if (ROOT / name).exists()]
    assert not present, f"forbidden mypy-config files at the repo root: {present}"
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    nested = [name for name in tracked if name and Path(name).name in forbidden]
    assert not nested, f"forbidden mypy-config files tracked in git: {nested}"


# --------------------------------------------------------------------------------------------
# the formatted tree
# --------------------------------------------------------------------------------------------
def test_ruff_format_check_passes_for_the_whole_repo():
    done = _ruff("format", "--check", "--output-format", "concise", ".")
    assert done.returncode == 0, "\n".join((done.stdout + done.stderr).splitlines()[:30])


def test_a_second_ruff_format_run_would_produce_no_diff():
    done = _ruff("format", "--diff", ".")
    assert done.returncode == 0, "\n".join((done.stdout + done.stderr).splitlines()[:30])
    assert done.stdout.strip() == "", (
        f"ruff format still wants to change files:\n{done.stdout[:2000]}"
    )
