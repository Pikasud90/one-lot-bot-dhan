"""US-003 mode B: the 80 percent coverage gate for the new module, measured on the
Tester's acceptance tests ALONE (the pair's own unit tests under tests/notify are not run).

The working agreements gate new core modules at 80 percent. The pair measured 99 to 100
percent with its own tests; this is the independent number. It runs a child pytest with
pytest-cov over the other US-003 acceptance files (never this one, so it cannot recurse) and
reads the JSON report. Line and branch coverage are both measured.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MODULE = "onelot.notify.telegram"
THRESHOLD = 80.0


def _acceptance_files() -> list[str]:
    own = Path(__file__).name
    skip = {own, "test_us003_modeb_desk_end_to_end.py"}  # the desk harness is slow and adds nothing
    return sorted(str(p) for p in HERE.glob("test_us003_*.py") if p.name not in skip)


@pytest.mark.slow  # reruns about 1000 tests under coverage (~20 s); the US-003 Reviewer asked
def test_the_acceptance_tests_alone_cover_the_telegram_module_at_least_80_percent(tmp_path):
    report = tmp_path / "coverage.json"
    env = dict(os.environ, COVERAGE_FILE=str(tmp_path / ".coverage"))
    env.pop("PYTEST_ADDOPTS", None)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *_acceptance_files(),
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "-q",
        "--no-header",
        "-W",
        "ignore",
        f"--cov={MODULE}",
        "--cov-branch",
        f"--cov-report=json:{report}",
    ]
    done = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600, check=False
    )
    assert report.exists(), f"no coverage report (is pytest-cov installed?)\n{done.stdout[-1500:]}"
    files = json.loads(report.read_text(encoding="utf-8"))["files"]
    (name,) = [n for n in files if n.replace(os.sep, "/").endswith("onelot/notify/telegram.py")]
    summary = files[name]["summary"]
    percent = float(summary["percent_covered"])
    detail = (
        f"{summary['covered_lines']}/{summary['num_statements']} lines, "
        f"{summary['covered_branches']}/{summary['num_branches']} branches"
    )
    assert percent >= THRESHOLD, f"telegram.py is {percent:.1f}% covered ({detail})"
