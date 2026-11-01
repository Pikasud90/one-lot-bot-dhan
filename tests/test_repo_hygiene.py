"""Repository hygiene: a fresh clone must be complete.

Regression: an unanchored ``data/`` rule in .gitignore also matched the source
package ``src/onelot/data/``. Every local run passed (the files were on disk) while
a fresh clone could not even import the engine. These tests look at what git
tracks, not at what happens to be on this disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRESH_CLONE_IMPORTS = ("onelot.engine", "onelot.lab.runner", "onelot.desk.desk")
GIT_TIMEOUT_S = 60


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout; raise with git's stderr on failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def _is_git_checkout(repo: Path) -> bool:
    if shutil.which("git") is None:
        return False
    try:
        return _git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except RuntimeError:
        return False


def untracked_sources(repo: Path) -> list[str]:
    """POSIX paths of ``src/**/*.py`` files on disk that git does not track."""
    src = repo / "src"
    tracked = set(_git(repo, "ls-files", "-z", "--", "src").split("\0")) - {""}
    on_disk = {
        p.relative_to(repo).as_posix() for p in src.rglob("*.py") if "__pycache__" not in p.parts
    }
    return sorted(on_disk - tracked)


def _explain(repo: Path, paths: list[str]) -> str:
    """One line per path, naming the .gitignore rule that hides it when there is one."""
    lines = []
    for rel in paths:
        proc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-v", "--", rel],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        rule = proc.stdout.strip()
        lines.append(
            f"{rel}  <- ignored by {rule}" if rule else f"{rel}  <- not added (git add it)"
        )
    return "\n  ".join(lines)


needs_git = pytest.mark.skipif(not _is_git_checkout(ROOT), reason="not a git checkout")


@needs_git
def test_every_source_file_is_tracked_by_git() -> None:
    missing = untracked_sources(ROOT)
    assert not missing, "source files a fresh clone would NOT contain:\n  " + _explain(
        ROOT, missing
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_the_guard_catches_an_ignore_rule_that_hides_source(tmp_path: Path) -> None:
    """Guards the guard: replay the original defect in a scratch repository."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg" / "data").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "pkg" / "data" / "__init__.py").write_text("", encoding="utf-8")
    (repo / ".gitignore").write_text("data/\n", encoding="utf-8")  # the unanchored rule
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all")

    missing = untracked_sources(repo)

    assert missing == ["src/pkg/data/__init__.py"]
    assert ".gitignore:1:data/" in _explain(repo, missing)


@needs_git
def test_a_fresh_clone_of_head_imports_the_entry_points(tmp_path: Path) -> None:
    head = _git(ROOT, "rev-parse", "HEAD").strip()
    clone = tmp_path / "clone"
    # --shared borrows the object store: no copy, no network, only committed content.
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(clone)],
        check=True,
        timeout=GIT_TIMEOUT_S,
    )
    _git(clone, "checkout", "--quiet", "--detach", head)

    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(clone / "src")
    probe = (
        "import importlib, sys\n"
        "for name in sys.argv[1:]:\n"
        "    mod = importlib.import_module(name)\n"
        "    print(mod.__file__)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, *FRESH_CLONE_IMPORTS],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, f"fresh clone of {head[:8]} cannot import:\n{proc.stderr}"
    files = [Path(line) for line in proc.stdout.splitlines()]
    assert len(files) == len(FRESH_CLONE_IMPORTS)
    # The venv has an editable install of this working tree; prove the clone was imported.
    assert all(f.resolve().is_relative_to(clone.resolve()) for f in files), files
