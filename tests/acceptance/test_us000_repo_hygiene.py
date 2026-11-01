"""US-000 AC1: every source file is tracked by git and a fresh clone of HEAD is importable.

Public contract: ``tests/test_repo_hygiene.py`` guards both properties.  The tests below state
the same properties independently, so they keep holding even if the hygiene module is weakened.
They use only git and the import system: no network, no credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HYGIENE_TEST = ROOT / "tests" / "test_repo_hygiene.py"
SMOKE_MODULES = ("onelot.engine", "onelot.lab.runner", "onelot.desk.desk")


def _clean_env(home: Path | None = None) -> dict[str, str]:
    """A minimal environment: no inherited secrets, no PYTHONPATH, no bytecode litter."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if home is not None:
        env["HOME"] = str(home)
    if "SYSTEMROOT" in os.environ:  # Windows needs it to start any process
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _git(*args: str, cwd: Path = ROOT) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()}"
    return done.stdout


def _tracked(prefix: str) -> set[str]:
    return {name for name in _git("ls-files", "-z", "--", prefix).split("\0") if name}


def _src_python_files() -> list[str]:
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_every_python_file_under_src_is_tracked_by_git():
    on_disk = _src_python_files()
    assert on_disk, "no .py files found under src/ (wrong repo root?)"
    tracked = _tracked("src")
    missing = [name for name in on_disk if name not in tracked]
    assert not missing, f"source files unknown to git (hidden by an ignore rule?): {missing}"


def test_no_ignore_rule_hides_a_python_file_under_src():
    listing = _git("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", "src")
    hidden = [name for name in listing.split("\0") if name.endswith(".py")]
    assert not hidden, f"ignore rules hide source files from git: {hidden}"


def test_onelot_data_package_is_tracked_the_original_incident():
    # An unanchored `data/` ignore rule once hid src/onelot/data from git: a fresh clone could
    # not start.  Pin that exact package so a recurrence names the culprit directly.
    tracked = _tracked("src/onelot/data")
    assert "src/onelot/data/__init__.py" in tracked, "src/onelot/data is not tracked by git"
    assert len(tracked) >= 5, f"src/onelot/data lost most of its modules in git: {sorted(tracked)}"


def test_repo_hygiene_test_module_exists_and_collects_both_guards():
    assert HYGIENE_TEST.is_file(), "tests/test_repo_hygiene.py is missing"
    source = HYGIENE_TEST.read_text(encoding="utf-8")
    assert "ls-files" in source, "hygiene module does not use `git ls-files`"
    assert "clone" in source, "hygiene module has no fresh-clone smoke test"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            str(HYGIENE_TEST),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(),
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    collected = [line for line in done.stdout.splitlines() if "::" in line]
    assert len(collected) >= 2, f"expected a tracked-files test and a clone test, got {collected}"


@pytest.mark.slow
def test_repo_hygiene_test_module_passes_when_run_for_real():
    assert HYGIENE_TEST.is_file(), "tests/test_repo_hygiene.py is missing"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider", "-q"]
        + [str(HYGIENE_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_clean_env(),
        check=False,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]


@pytest.mark.slow
def test_fresh_clone_of_head_imports_engine_lab_runner_and_desk(tmp_path):
    head = _git("rev-parse", "HEAD").strip()
    clone = tmp_path / "fresh clone"
    _git("clone", "--quiet", "--no-hardlinks", str(ROOT), str(clone), cwd=tmp_path)
    _git("checkout", "--quiet", "--detach", head, cwd=clone)
    clone_src = (clone / "src").resolve()
    workdir = tmp_path / "empty cwd"
    workdir.mkdir()
    probe = textwrap.dedent(
        """
        import importlib, os, sys

        src = sys.argv[1]
        sys.path.insert(0, src)
        problems = []
        for name in sys.argv[2:]:
            try:
                module = importlib.import_module(name)
            except Exception as exc:  # report every failure, not just the first
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            where = os.path.realpath(module.__file__)
            if not where.startswith(src):
                problems.append(f"{name}: imported from {where}, not from the clone")
        if problems:
            print("\\n".join(problems))
            sys.exit(1)
        print("imported:", ", ".join(sys.argv[2:]))
        """
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(clone_src), *SMOKE_MODULES],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=300,
        env=_clean_env(workdir),
        check=False,
    )
    assert done.returncode == 0, (
        f"a fresh clone of {head[:10]} cannot import the app:\n{done.stdout}{done.stderr}"
    )
