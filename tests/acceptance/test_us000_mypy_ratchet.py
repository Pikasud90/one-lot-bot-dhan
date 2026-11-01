"""US-000 AC3: the per-file mypy ratchet (``scripts/mypy_ratchet.py``).

Contract under test (story file, Public contract + Manager amendment):

* exit codes: 0 ok | 1 regression | 2 tool error (mypy missing, crash, unparsable output,
  "errors prevented further checking", version mismatch, unusable baseline);
* a file absent from the baseline counts as 0; a decrease exits 0 and prints ``run --update``;
* ``--update`` never raises a count unless ``--allow-regression`` is given;
* a baseline entry for a deleted file warns;
* baseline JSON: ``{"_meta": {"mypy_version", "flags"}, "files": {posix path: int}}``;
* mypy runs as ``[sys.executable, "-m", "mypy", *flags]`` with cwd=<root>; ``--root`` and
  ``--baseline`` override the defaults.

Everything is hermetic: a fake ``mypy`` package is put first on PYTHONPATH of the subprocess and
prints canned output (and ``mypy x.y.z`` for ``--version``).  The real mypy is never imported, so
these tests behave the same whether or not mypy is installed in the venv.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
RATCHET = ROOT / "scripts" / "mypy_ratchet.py"
TRACKED_BASELINE = ROOT / "scripts" / "mypy_baseline.json"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"

FAKE_VERSION = "1.11.2"
OLD_VERSION = "1.10.0"
ALPHA = "src/onelot/alpha.py"
BETA = "src/onelot/beta.py"
GAMMA = "src/onelot/lab/gamma.py"
DELTA = "src/onelot/desk/delta.py"
POOL = (ALPHA, BETA, GAMMA, DELTA)
REMOVED = "src/onelot/removed.py"

FAKE_MYPY_MAIN = """
import json
import os
import sys
from pathlib import Path

site = Path(__file__).resolve().parent.parent
scenario = json.loads((site / "scenario.json").read_text(encoding="utf-8"))
argv = sys.argv[1:]
with (site / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")
if "--version" in argv:
    sys.stdout.write(scenario["version_stdout"])
    sys.exit(scenario["version_exit"])
if scenario["crash"]:
    sys.stderr.write("INTERNAL ERROR: simulated mypy crash\\n")
    sys.exit(scenario["exit"])
if scenario.get("stdout_hex") is not None:
    sys.stdout.flush()
    sys.stdout.buffer.write(bytes.fromhex(scenario["stdout_hex"]))
    sys.stdout.flush()
    sys.exit(scenario["exit"])
sys.stdout.write(scenario["stdout"])
sys.stderr.write(scenario["stderr"])
sys.exit(scenario["exit"])
"""

# Makes `python -m mypy` fail exactly as it does when mypy is not installed, even when the venv
# has it: the path finder is told that no module called mypy exists.
BLOCK_MYPY_SITECUSTOMIZE = """
import importlib.machinery
import sys


class _NoMypy(importlib.machinery.PathFinder):
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname == "mypy" or fullname.startswith("mypy."):
            return None
        return super().find_spec(fullname, path, target)


sys.meta_path[:] = [_NoMypy if f is importlib.machinery.PathFinder else f for f in sys.meta_path]
"""


# --------------------------------------------------------------------------------------------
# canned mypy output
# --------------------------------------------------------------------------------------------
def error_line(path: str, line: int) -> str:
    message = (
        'Incompatible types in assignment (expression has type "str", variable has type "int")'
    )
    return f"{path}:{line}: error: {message}  [assignment]"


def summary_line(errors: int, files: int, *, checked: int = 40, prevented: bool = False) -> str:
    if errors == 0 and not prevented:
        return f"Success: no issues found in {checked} source files"
    tail = "errors prevented further checking" if prevented else f"checked {checked} source files"
    noun_e = "error" if errors == 1 else "errors"
    noun_f = "file" if files == 1 else "files"
    return f"Found {errors} {noun_e} in {files} {noun_f} ({tail})"


def render(
    counts: dict[str, int],
    *,
    sep: str = "/",
    newline: str = "\n",
    summary: str | None = "auto",
    reverse: bool = False,
) -> str:
    """mypy-style stdout for ``counts`` (path -> number of errors)."""
    lines: list[str] = []
    paths = sorted(counts, reverse=reverse)
    for path in paths:
        numbers = range(1, counts[path] + 1)
        for number in reversed(numbers) if reverse else numbers:
            lines.append(error_line(path.replace("/", sep), number * 3))
    if summary == "auto":
        lines.append(summary_line(sum(counts.values()), len(paths)))
    elif summary is not None:
        lines.append(summary)
    return "".join(line + newline for line in lines)


# --------------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------------
@dataclass
class Run:
    code: int
    stdout: str
    stderr: str
    calls: list[dict]

    @property
    def text(self) -> str:
        return f"{self.stdout}\n{self.stderr}"

    @property
    def check_calls(self) -> list[dict]:
        return [call for call in self.calls if "--version" not in call["argv"]]


class Sandbox:
    """A throw-away repo root (with spaces in its name, like the real one) plus a fake mypy."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "ONE LOT BOT - DHAN"
        self.root.mkdir()
        self.workdir = base / "somewhere else"
        self.workdir.mkdir()
        self.baseline = base / "b.json"
        self.fake_site = base / "fake_site"
        (self.fake_site / "mypy").mkdir(parents=True)
        (self.fake_site / "mypy" / "__init__.py").write_text("", encoding="utf-8")
        (self.fake_site / "mypy" / "__main__.py").write_text(FAKE_MYPY_MAIN, encoding="utf-8")
        self.block_site = base / "block_site"
        self.block_site.mkdir()
        (self.block_site / "sitecustomize.py").write_text(BLOCK_MYPY_SITECUSTOMIZE, "utf-8")
        for rel in (*POOL, "src/onelot/other.py"):
            self.touch(rel)
        self.mypy(stdout=render({}), exit_code=0)

    def touch(self, rel: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x: int = 1\n", encoding="utf-8")

    def mypy(
        self,
        *,
        stdout: str,
        exit_code: int,
        stderr: str = "",
        version_stdout: str | None = None,
        version_exit: int = 0,
        crash: bool = False,
        stdout_hex: str | None = None,
    ) -> None:
        scenario = {
            "stdout_hex": stdout_hex,
            "stdout": stdout,
            "stderr": stderr,
            "exit": exit_code,
            "crash": crash,
            "version_stdout": (
                f"mypy {FAKE_VERSION} (compiled: yes)\n"
                if version_stdout is None
                else version_stdout
            ),
            "version_exit": version_exit,
        }
        (self.fake_site / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")

    def mypy_counts(self, counts: dict[str, int], **render_kwargs) -> None:
        exit_code = 1 if counts else 0
        self.mypy(stdout=render(counts, **render_kwargs), exit_code=exit_code)

    def set_baseline(
        self,
        files: dict[str, int],
        *,
        version: str = FAKE_VERSION,
        flags: list[str] | None = None,
    ) -> None:
        meta = {"mypy_version": version, "flags": ratchet_flags() if flags is None else flags}
        self.baseline.write_text(
            json.dumps({"_meta": meta, "files": files}, indent=2) + "\n", encoding="utf-8"
        )

    def baseline_bytes(self) -> bytes:
        return self.baseline.read_bytes()

    def baseline_json(self) -> dict:
        return json.loads(self.baseline.read_text(encoding="utf-8"))

    def install_scripts_copy(self) -> Path:
        """Put a copy of scripts/ inside the sandbox root; the baseline moves next to it."""
        dest = self.root / "scripts"
        shutil.copytree(ROOT / "scripts", dest)
        shutil.copy2(RATCHET, dest / "mypy_ratchet.py")
        self.baseline = dest / "mypy_baseline.json"
        return dest / "mypy_ratchet.py"

    def run(
        self,
        *args: str,
        hide_mypy: bool = False,
        script: Path | None = None,
        with_paths: bool = True,
        umask: int | None = None,
    ) -> Run:
        assert RATCHET.is_file(), "scripts/mypy_ratchet.py does not exist yet (US-000 AC3)"
        calls_log = self.fake_site / "calls.jsonl"
        calls_log.unlink(missing_ok=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "MYPY", "VIRTUAL_ENV"))
        }
        env["PYTHONPATH"] = str(self.block_site if hide_mypy else self.fake_site)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [sys.executable, str(script or RATCHET)]
        if with_paths:
            command += ["--root", str(self.root), "--baseline", str(self.baseline)]
        command += list(args)
        if umask is not None:  # POSIX only: run the ratchet under a chosen umask
            command = [
                "/bin/sh",
                "-c",
                'umask "$1"; shift; exec "$@"',
                "sh",
                f"{umask:04o}",
                *command,
            ]
        try:
            done = subprocess.run(
                command,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=90,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - only on a hung ratchet
            pytest.fail(f"mypy_ratchet.py hung (more than 90 s): {exc}")
        calls = []
        if calls_log.exists():
            calls = [json.loads(row) for row in calls_log.read_text("utf-8").splitlines() if row]
        return Run(done.returncode, done.stdout, done.stderr, calls)


@functools.lru_cache(maxsize=1)
def ratchet_flags() -> list[str]:
    """The flags the ratchet runs mypy with, learnt from a throw-away ``--update``.

    The contract says the flags are recorded in ``_meta.flags``.  Baselines written by these
    tests reuse them, so the tests hold for an implementation that insists on identical flags
    and for one that does not.
    """
    with tempfile.TemporaryDirectory(prefix="us000_flags_") as tmp:
        probe = Sandbox(Path(tmp))
        run = probe.run("--update", "--allow-regression")
        assert run.code == 0, f"cannot learn the ratchet's flags: --update failed\n{run.text}"
        flags = probe.baseline_json()["_meta"]["flags"]
    assert isinstance(flags, list), f"_meta.flags must be a list, got {flags!r}"
    return flags


@pytest.fixture()
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_tool_error(run: Run) -> None:
    assert run.code == 2, f"expected exit 2 (tool error), got {run.code}\n{run.text}"
    assert run.text.strip(), "a tool error must say what went wrong"


def assert_regression(run: Run, *files: str) -> None:
    assert run.code == 1, f"expected exit 1 (regression), got {run.code}\n{run.text}"
    assert "Traceback (most recent call last)" not in run.text, run.text
    for path in files:
        assert path in run.text, f"regressed file {path} is not listed:\n{run.text}"


# --------------------------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------------------------
def test_help_documents_the_contract_flags(sb):
    run = sb.run("--help")
    assert run.code == 0, run.text
    for flag in ("--update", "--allow-regression", "--root", "--baseline"):
        assert flag in run.stdout, f"{flag} missing from --help:\n{run.stdout}"


def test_mypy_runs_once_as_python_dash_m_mypy_with_cwd_set_to_the_root(sb):
    sb.set_baseline({})
    sb.mypy_counts({})
    run = sb.run()
    assert run.code == 0, run.text
    assert len(run.check_calls) == 1, f"mypy must run exactly once, saw {run.calls}"
    assert Path(run.check_calls[0]["cwd"]).resolve() == sb.root.resolve()


def test_defaults_are_the_parent_of_scripts_as_root_and_the_baseline_next_to_the_script(sb):
    script = sb.install_scripts_copy()
    sb.set_baseline({ALPHA: 3})
    sb.mypy_counts({ALPHA: 1})
    run = sb.run(script=script, with_paths=False)
    assert run.code == 0, run.text
    assert len(run.check_calls) == 1
    assert Path(run.check_calls[0]["cwd"]).resolve() == sb.root.resolve()
    assert "run --update" in run.text
    sb.mypy_counts({ALPHA: 4})
    assert_regression(sb.run(script=script, with_paths=False), ALPHA)


# --------------------------------------------------------------------------------------------
# exit 0: nothing got worse
# --------------------------------------------------------------------------------------------
def test_exits_0_when_mypy_finds_no_issues_and_the_baseline_is_empty(sb):
    sb.set_baseline({})
    sb.mypy_counts({})
    run = sb.run()
    assert run.code == 0, run.text
    assert len(run.check_calls) == 1


def test_exits_0_when_every_file_matches_its_baseline_count(sb):
    sb.set_baseline({ALPHA: 2, BETA: 1})
    sb.mypy_counts({ALPHA: 2, BETA: 1})
    run = sb.run()
    assert run.code == 0, run.text
    assert len(run.check_calls) == 1


@pytest.mark.parametrize(
    "version_stdout",
    [
        f"mypy {FAKE_VERSION}\n",
        f"mypy {FAKE_VERSION} (compiled: yes)\n",
        f"mypy {FAKE_VERSION} (compiled: no)\n",
    ],
)
def test_real_mypy_version_banners_are_understood(sb, version_stdout):
    sb.set_baseline({ALPHA: 1})
    sb.mypy(stdout=render({ALPHA: 1}), exit_code=1, version_stdout=version_stdout)
    run = sb.run()
    assert run.code == 0, run.text


def test_a_decrease_exits_0_and_tells_the_owner_to_run_update(sb):
    sb.set_baseline({ALPHA: 3, BETA: 2})
    sb.mypy_counts({ALPHA: 1, BETA: 2})
    run = sb.run()
    assert run.code == 0, run.text
    assert "run --update" in run.text, run.text


def test_a_file_that_became_clean_is_a_decrease_not_a_failure(sb):
    sb.set_baseline({ALPHA: 2})
    sb.mypy_counts({})
    run = sb.run()
    assert run.code == 0, run.text
    assert "run --update" in run.text, run.text


@pytest.mark.parametrize(
    ("baseline", "observed"),
    [
        ({ALPHA: 3, BETA: 2}, {ALPHA: 1, BETA: 2}),
        ({ALPHA: 2}, {ALPHA: 3}),
        ({ALPHA: 2}, {ALPHA: 2}),
        ({ALPHA: 1}, {ALPHA: 1, GAMMA: 1}),
    ],
)
def test_a_plain_check_never_modifies_the_baseline(sb, baseline, observed):
    sb.set_baseline(baseline)
    sb.mypy_counts(observed)
    before = sb.baseline_bytes()
    sb.run()
    assert sb.baseline_bytes() == before


def test_notes_are_not_counted_as_errors(sb):
    stdout = "".join(
        [
            error_line(ALPHA, 3) + "\n",
            f'{ALPHA}:3: note: Revealed type is "builtins.int"\n',
            f"{ALPHA}:9: note: See https://mypy.readthedocs.io/en/stable/error_code_list.html\n",
            summary_line(1, 1) + "\n",
        ]
    )
    sb.set_baseline({ALPHA: 1})
    sb.mypy(stdout=stdout, exit_code=1)
    run = sb.run()
    assert run.code == 0, run.text


def test_singular_summary_wording_is_parsed(sb):
    # Real mypy says "1 error in 1 file", not "1 errors in 1 files".
    stdout = error_line(ALPHA, 3) + "\nFound 1 error in 1 file (checked 40 source files)\n"
    sb.set_baseline({ALPHA: 1})
    sb.mypy(stdout=stdout, exit_code=1)
    run = sb.run()
    assert run.code == 0, run.text


def test_a_very_large_mypy_report_is_read_without_blocking(sb):
    counts = {ALPHA: 1500, BETA: 1500, GAMMA: 1500, DELTA: 1500}  # well over a pipe buffer
    sb.set_baseline(counts)
    sb.mypy_counts(counts)
    assert sb.run().code == 0
    sb.mypy_counts({**counts, DELTA: 1501})
    assert_regression(sb.run(), DELTA)


# --------------------------------------------------------------------------------------------
# exit 1: regression
# --------------------------------------------------------------------------------------------
def test_a_regression_exits_1_and_lists_every_regressed_file(sb):
    sb.set_baseline({ALPHA: 1, BETA: 2, GAMMA: 4})
    sb.mypy_counts({ALPHA: 2, BETA: 2, GAMMA: 5})
    assert_regression(sb.run(), ALPHA, GAMMA)


def test_a_new_error_in_a_file_absent_from_the_baseline_is_a_regression(sb):
    sb.set_baseline({ALPHA: 1})
    sb.mypy_counts({ALPHA: 1, GAMMA: 1})
    assert_regression(sb.run(), GAMMA)


def test_with_an_empty_baseline_the_first_error_anywhere_is_a_regression(sb):
    sb.set_baseline({})
    sb.mypy_counts({DELTA: 1})
    assert_regression(sb.run(), DELTA)


def test_a_regression_is_not_offset_by_improvements_elsewhere(sb):
    # The total went down (5 -> 2) but BETA got worse: the ratchet is per file.
    sb.set_baseline({ALPHA: 5})
    sb.mypy_counts({ALPHA: 1, BETA: 1})
    assert_regression(sb.run(), BETA)


@pytest.mark.parametrize(
    ("baseline", "observed", "expected"),
    [
        (1, 1, 0),
        (1, 2, 1),
        (5, 4, 0),
        (5, 5, 0),
        (5, 6, 1),
        (1000, 1001, 1),
        (1000, 999, 0),
    ],
)
def test_the_boundary_is_exactly_the_baseline_count(sb, baseline, observed, expected):
    sb.set_baseline({ALPHA: baseline})
    sb.mypy_counts({ALPHA: observed})
    run = sb.run()
    assert run.code == expected, run.text


def test_allow_regression_alone_does_not_turn_a_regression_into_success(sb):
    # The flag only changes what --update may do; on a plain check it must never bypass the gate.
    sb.set_baseline({ALPHA: 1})
    sb.mypy_counts({ALPHA: 2})
    before = sb.baseline_bytes()
    run = sb.run("--allow-regression")
    assert run.code != 0, run.text
    assert sb.baseline_bytes() == before


# --------------------------------------------------------------------------------------------
# exit 2: tool errors never look like success
# --------------------------------------------------------------------------------------------
def test_missing_mypy_exits_2_and_names_mypy(sb):
    sb.set_baseline({ALPHA: 99})
    run = sb.run(hide_mypy=True)
    assert_tool_error(run)
    assert "mypy" in run.text.lower()


@pytest.mark.parametrize("mypy_exit", [1, 2, 139])
def test_a_mypy_crash_exits_2(sb, mypy_exit):
    sb.set_baseline({ALPHA: 99, BETA: 99})
    sb.mypy(stdout="", exit_code=mypy_exit, crash=True)
    assert_tool_error(sb.run())


def test_a_crash_while_asking_for_the_version_exits_2(sb):
    sb.set_baseline({ALPHA: 99})
    sb.mypy(stdout=render({}), exit_code=0, version_stdout="", version_exit=1)
    assert_tool_error(sb.run())


@pytest.mark.parametrize("version_stdout", ["", "garbage\n", "mypy\n", "mypy banana\n"])
def test_an_unparsable_version_exits_2(sb, version_stdout):
    sb.set_baseline({})
    sb.mypy(stdout=render({}), exit_code=0, version_stdout=version_stdout)
    assert_tool_error(sb.run())


@pytest.mark.parametrize(
    ("stdout", "mypy_exit"),
    [
        ("", 0),
        ("", 1),
        ("", 2),
        ("hello world\n", 0),
        ("hello world\nand some more\n", 1),
        ("Segmentation fault: 11\n", 139),
    ],
)
def test_unparsable_output_exits_2(sb, stdout, mypy_exit):
    sb.set_baseline({ALPHA: 99})
    sb.mypy(stdout=stdout, exit_code=mypy_exit, stderr="mypy: something unexpected\n")
    assert_tool_error(sb.run())


def test_undecodable_mypy_output_is_a_tool_error_never_a_traceback_or_a_fake_regression(sb):
    # Invalid UTF-8 in mypy's report.  Parsing it leniently is fine (exit 0 here, the baseline is
    # generous); refusing it is fine (exit 2).  A crash with exit 1 would pose as a regression.
    sb.set_baseline({ALPHA: 99})
    raw = error_line(ALPHA, 3).encode() + b" \xff\xfe\n"
    raw += b"Found 1 error in 1 file (checked 3 source files)\n"
    sb.mypy(stdout="", exit_code=1, stdout_hex=raw.hex())
    run = sb.run()
    assert run.code in (0, 2), f"exit {run.code} for undecodable output\n{run.text[-800:]}"
    assert "Traceback (most recent call last)" not in run.text, run.text[-800:]


def test_error_lines_without_a_summary_exit_2_even_if_the_baseline_allows_them(sb):
    sb.set_baseline({ALPHA: 5})
    sb.mypy(stdout=render({ALPHA: 1}, summary=None), exit_code=1)
    assert_tool_error(sb.run())


@pytest.mark.parametrize("mypy_exit", [1, 2])
def test_errors_prevented_further_checking_exits_2_even_when_counts_look_fine(sb, mypy_exit):
    # The count matches the baseline exactly, so only the blocking-error marker can fail this.
    stdout = (
        f"{ALPHA}:5: error: invalid syntax  [syntax]\n" + summary_line(1, 1, prevented=True) + "\n"
    )
    sb.set_baseline({ALPHA: 1})
    sb.mypy(stdout=stdout, exit_code=mypy_exit)
    assert_tool_error(sb.run())


@pytest.mark.parametrize(
    ("counts", "summary"),
    [
        ({ALPHA: 2}, summary_line(3, 1)),  # more errors announced than printed
        ({ALPHA: 2}, summary_line(1, 1)),  # fewer errors announced than printed
        ({ALPHA: 2}, summary_line(2, 2)),  # files disagree
        ({ALPHA: 1, BETA: 1}, summary_line(2, 1)),  # files disagree the other way
        ({}, summary_line(1, 1)),  # announced errors, none printed
        ({ALPHA: 1}, summary_line(0, 0)),  # "Success" while an error line was printed
    ],
)
def test_a_summary_that_disagrees_with_the_parsed_lines_exits_2(sb, counts, summary):
    sb.set_baseline({ALPHA: 99, BETA: 99})
    sb.mypy(stdout=render(counts, summary=summary), exit_code=1 if counts else 0)
    assert_tool_error(sb.run())


def unusable_baseline(kind: str) -> str:
    good = {"mypy_version": FAKE_VERSION, "flags": ratchet_flags()}
    broken: dict[str, str] = {
        "empty file": "",
        "invalid JSON": "{not json",
        "top level is not an object": "[]",
        "no _meta": json.dumps({"files": {}}),
        "no files": json.dumps({"_meta": good}),
        "no mypy_version": json.dumps({"_meta": {"flags": good["flags"]}, "files": {}}),
        "files is a list": json.dumps({"_meta": good, "files": [1, 2]}),
        "string count": json.dumps({"_meta": good, "files": {ALPHA: "3"}}),
        "negative count": json.dumps({"_meta": good, "files": {ALPHA: -1}}),
        "fractional count": json.dumps({"_meta": good, "files": {ALPHA: 1.5}}),
    }
    return broken[kind]


@pytest.mark.parametrize(
    "kind",
    [
        "empty file",
        "invalid JSON",
        "top level is not an object",
        "no _meta",
        "no files",
        "no mypy_version",
        "files is a list",
        "string count",
        "negative count",
        "fractional count",
    ],
)
def test_an_unusable_baseline_exits_2_not_1(sb, kind):
    # Exit 1 means "regression"; a broken baseline is a tool error and must not pose as one.
    sb.baseline.write_text(unusable_baseline(kind), encoding="utf-8")
    sb.mypy_counts({})
    run = sb.run()
    assert_tool_error(run)
    assert "Traceback (most recent call last)" not in run.text, f"{kind}:\n{run.text}"


def test_a_missing_baseline_never_exits_0_when_mypy_reports_errors(sb):
    assert not sb.baseline.exists()
    sb.mypy_counts({ALPHA: 1})
    run = sb.run()
    assert run.code != 0, run.text
    assert "baseline" in run.text.lower(), run.text


@pytest.mark.parametrize(
    ("baseline", "observed"),
    [
        ({}, {}),  # all clean
        ({ALPHA: 2}, {ALPHA: 2}),  # equal
        ({ALPHA: 3}, {ALPHA: 1}),  # decrease
        ({ALPHA: 1}, {ALPHA: 2}),  # regression
    ],
)
def test_a_version_mismatch_is_never_a_pass_and_names_both_versions(sb, baseline, observed):
    sb.set_baseline(baseline, version=OLD_VERSION)
    sb.mypy_counts(observed)
    run = sb.run()
    assert run.code in (1, 2), f"version mismatch must fail, got {run.code}\n{run.text}"
    assert OLD_VERSION in run.text and FAKE_VERSION in run.text, run.text


def test_a_patch_level_version_difference_is_still_a_mismatch(sb):
    sb.set_baseline({}, version="1.11.1")
    sb.mypy_counts({})
    run = sb.run()
    assert run.code in (1, 2), run.text


# --------------------------------------------------------------------------------------------
# paths: POSIX in the baseline, whatever mypy printed
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_windows_style_paths_match_a_posix_baseline(sb, newline):
    sb.set_baseline({ALPHA: 1, GAMMA: 2})
    sb.mypy_counts({ALPHA: 1, GAMMA: 2}, sep="\\", newline=newline)
    run = sb.run()
    assert run.code == 0, run.text
    sb.mypy_counts({ALPHA: 1, GAMMA: 3}, sep="\\", newline=newline)
    assert_regression(sb.run())


def test_a_windows_style_path_missing_from_the_baseline_is_a_regression(sb):
    sb.set_baseline({ALPHA: 1})
    sb.mypy_counts({ALPHA: 1, GAMMA: 1}, sep="\\")
    assert_regression(sb.run())


def test_update_writes_posix_paths_even_when_mypy_printed_backslashes(sb):
    sb.mypy_counts({GAMMA: 2, ALPHA: 1}, sep="\\", newline="\r\n")
    run = sb.run("--update", "--allow-regression")
    assert run.code == 0, run.text
    assert sb.baseline_json()["files"] == {ALPHA: 1, GAMMA: 2}
    assert "\\\\" not in sb.baseline.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# deleted files
# --------------------------------------------------------------------------------------------
def test_a_baseline_entry_for_a_deleted_file_warns_but_does_not_fail(sb):
    assert not (sb.root / REMOVED).exists()
    sb.set_baseline({ALPHA: 1, REMOVED: 2})
    sb.mypy_counts({ALPHA: 1})
    run = sb.run()
    assert run.code == 0, run.text
    assert REMOVED in run.text, f"the warning must name the deleted file:\n{run.text}"
    assert "warn" in run.text.lower(), run.text


def test_a_deleted_file_warning_does_not_hide_a_real_regression(sb):
    sb.set_baseline({ALPHA: 1, REMOVED: 2})
    sb.mypy_counts({ALPHA: 2})
    assert_regression(sb.run(), ALPHA)


# --------------------------------------------------------------------------------------------
# --update
# --------------------------------------------------------------------------------------------
def test_update_creates_a_baseline_with_the_documented_shape(sb):
    assert not sb.baseline.exists()
    sb.mypy_counts({BETA: 2, ALPHA: 1})
    run = sb.run("--update", "--allow-regression")
    assert run.code == 0, run.text
    data = sb.baseline_json()
    assert set(data) == {"_meta", "files"}, f"unexpected top-level keys: {sorted(data)}"
    assert {"mypy_version", "flags"} <= set(data["_meta"])
    assert data["_meta"]["mypy_version"] == FAKE_VERSION
    assert isinstance(data["_meta"]["flags"], list)
    assert all(isinstance(flag, str) for flag in data["_meta"]["flags"])
    assert data["files"] == {ALPHA: 1, BETA: 2}
    for path, count in data["files"].items():
        assert "\\" not in path and not path.startswith("/")
        assert type(count) is int and count >= 1


def test_update_records_the_flags_mypy_was_invoked_with_in_the_baseline(sb):
    sb.mypy_counts({ALPHA: 1})
    run = sb.run("--update", "--allow-regression")
    assert run.code == 0, run.text
    assert len(run.check_calls) == 1
    assert sb.baseline_json()["_meta"]["flags"] == run.check_calls[0]["argv"]
    assert Path(run.check_calls[0]["cwd"]).resolve() == sb.root.resolve()


def test_update_after_an_improvement_lowers_counts_and_prunes_cleared_and_deleted_files(sb):
    sb.set_baseline({ALPHA: 3, BETA: 1, REMOVED: 2})
    sb.mypy_counts({ALPHA: 1})
    run = sb.run("--update")
    assert run.code == 0, run.text
    assert sb.baseline_json()["files"] == {ALPHA: 1}


def test_update_with_nothing_changed_is_a_no_op_that_succeeds(sb):
    sb.set_baseline({ALPHA: 2})
    sb.mypy_counts({ALPHA: 2})
    run = sb.run("--update")
    assert run.code == 0, run.text
    assert sb.baseline_json()["files"] == {ALPHA: 2}


@pytest.mark.parametrize(
    ("baseline", "observed", "culprit"),
    [
        ({ALPHA: 1, BETA: 5}, {ALPHA: 2, BETA: 1}, ALPHA),  # one up, one down, total down
        ({ALPHA: 1}, {ALPHA: 1, GAMMA: 1}, GAMMA),  # a new file counts as raised from 0
        ({}, {DELTA: 3}, DELTA),
    ],
)
def test_update_refuses_to_raise_any_count_without_allow_regression(
    sb, baseline, observed, culprit
):
    sb.set_baseline(baseline)
    sb.mypy_counts(observed)
    before = sb.baseline_bytes()
    run = sb.run("--update")
    assert run.code != 0, f"--update raised a count without --allow-regression\n{run.text}"
    assert "Traceback (most recent call last)" not in run.text, run.text
    assert culprit in run.text, f"the refusal must name {culprit}:\n{run.text}"
    assert sb.baseline_bytes() == before, "a refused --update must leave the baseline untouched"


def test_update_with_allow_regression_accepts_higher_counts(sb):
    sb.set_baseline({ALPHA: 1, BETA: 5})
    sb.mypy_counts({ALPHA: 2, BETA: 1, GAMMA: 1})
    run = sb.run("--update", "--allow-regression")
    assert run.code == 0, run.text
    assert sb.baseline_json()["files"] == {ALPHA: 2, BETA: 1, GAMMA: 1}


def test_after_update_the_same_mypy_output_is_green(sb):
    sb.set_baseline({ALPHA: 1})
    sb.mypy_counts({ALPHA: 4, BETA: 2})
    assert sb.run("--update", "--allow-regression").code == 0
    assert sb.run().code == 0


def test_update_adopts_a_new_mypy_version_when_no_count_rises(sb):
    sb.set_baseline({ALPHA: 2}, version=OLD_VERSION)
    sb.mypy_counts({ALPHA: 2})
    run = sb.run("--update")
    assert run.code == 0, run.text
    assert sb.baseline_json()["_meta"]["mypy_version"] == FAKE_VERSION
    assert sb.run().code == 0


def test_update_output_is_deterministic_whatever_order_mypy_printed_in(sb):
    counts = {GAMMA: 2, ALPHA: 3, BETA: 1}
    sb.mypy_counts(counts)
    assert sb.run("--update", "--allow-regression").code == 0
    first = sb.baseline_bytes()
    sb.baseline.unlink()
    sb.mypy_counts(counts, reverse=True)
    assert sb.run("--update", "--allow-regression").code == 0
    assert sb.baseline_bytes() == first, "baseline must be diff-stable in git"


def test_update_that_cannot_write_the_baseline_is_a_tool_error_not_a_traceback(sb):
    # Exit 1 means "refused to raise a count"; a failed write is a tool error (exit 2).
    sb.mypy_counts({ALPHA: 1})
    blocker = sb.base / "a file, not a directory"
    blocker.write_text("x", encoding="utf-8")
    sb.baseline = blocker / "b.json"
    run = sb.run("--update", "--allow-regression")
    assert_tool_error(run)
    assert "Traceback (most recent call last)" not in run.text, run.text[-800:]


@pytest.mark.parametrize(
    "scenario",
    ["crash", "prevented", "unparsable", "no_summary", "missing_mypy"],
)
def test_update_never_writes_a_baseline_from_a_failed_mypy_run(sb, scenario):
    sb.set_baseline({ALPHA: 3})
    before = sb.baseline_bytes()
    hide = False
    if scenario == "crash":
        sb.mypy(stdout="", exit_code=2, crash=True)
    elif scenario == "prevented":
        sb.mypy(
            stdout=f"{ALPHA}:5: error: invalid syntax  [syntax]\n"
            + summary_line(1, 1, prevented=True)
            + "\n",
            exit_code=2,
        )
    elif scenario == "unparsable":
        sb.mypy(stdout="hello world\n", exit_code=1)
    elif scenario == "no_summary":
        sb.mypy(stdout=render({ALPHA: 1}, summary=None), exit_code=1)
    else:
        hide = True
    run = sb.run("--update", "--allow-regression", hide_mypy=hide)
    assert_tool_error(run)
    assert sb.baseline_bytes() == before, "a failed mypy run must never rewrite the baseline"


# --------------------------------------------------------------------------------------------
# mode B: bootstrap, flags, file mode, quoting (regression tests for the review fixes)
# --------------------------------------------------------------------------------------------
def test_update_with_no_baseline_refuses_any_error_without_allow_regression(sb):
    # A missing baseline counts as an empty one: bootstrapping with errors is a deliberate act.
    assert not sb.baseline.exists()
    sb.mypy_counts({ALPHA: 1, BETA: 2})
    run = sb.run("--update")
    assert run.code == 1, run.text
    assert ALPHA in run.text and BETA in run.text, run.text
    assert "Traceback (most recent call last)" not in run.text, run.text
    assert not sb.baseline.exists(), "a refused bootstrap must not create the baseline"


def test_deleting_the_baseline_cannot_launder_existing_errors_through_update(sb):
    sb.set_baseline({ALPHA: 1, BETA: 1})
    sb.mypy_counts({ALPHA: 1, BETA: 1})
    sb.baseline.unlink()
    run = sb.run("--update")
    assert run.code == 1, run.text
    assert not sb.baseline.exists()


def test_update_with_no_baseline_and_a_clean_mypy_creates_an_empty_baseline(sb):
    sb.mypy_counts({})
    run = sb.run("--update")
    assert run.code == 0, run.text
    data = sb.baseline_json()
    assert data["files"] == {}
    assert data["_meta"]["mypy_version"] == FAKE_VERSION
    assert data["_meta"]["flags"] == ratchet_flags()
    assert sb.run().code == 0


@pytest.mark.parametrize("clean", [True, False])
def test_a_check_with_no_baseline_is_a_tool_error_even_when_mypy_is_clean(sb, clean):
    sb.mypy_counts({} if clean else {ALPHA: 1})
    run = sb.run()
    assert run.code == 2, run.text
    assert "--update" in run.text, "the error should say how to create the baseline"
    assert not sb.baseline.exists(), "a check must never create the baseline"


def _flag_variant(kind: str, flags: list[str]) -> list[str]:
    if kind == "suppressing flag added":
        return [*flags, "--ignore-errors"]
    if kind == "exclude added":
        return [*flags, "--exclude", "src"]
    if kind == "first flag dropped":
        return flags[1:]
    if kind == "reordered":
        return [*reversed(flags)] if len(set(flags)) > 1 else [*flags, "--x"]
    return []


@pytest.mark.parametrize(
    "kind",
    ["suppressing flag added", "exclude added", "first flag dropped", "reordered", "empty"],
)
def test_a_baseline_recorded_with_other_flags_is_a_tool_error_and_those_flags_never_run(sb, kind):
    flags = ratchet_flags()
    other = _flag_variant(kind, flags)
    assert other != flags, "test precondition: the variant must differ"
    sb.set_baseline({ALPHA: 5}, flags=other)
    sb.mypy_counts({ALPHA: 1})  # would look like a green decrease if the recorded flags were run
    before = sb.baseline_bytes()
    run = sb.run()
    assert_tool_error(run)
    assert "flag" in run.text.lower(), run.text
    assert all(call["argv"] == flags for call in run.check_calls), (
        f"mypy ran with flags other than the script's: {run.check_calls}"
    )
    assert sb.baseline_bytes() == before


def test_update_over_a_baseline_recorded_with_other_flags_adopts_the_script_flags(sb):
    flags = ratchet_flags()
    sb.set_baseline({ALPHA: 2}, flags=[*flags, "--ignore-errors"])
    sb.mypy_counts({ALPHA: 2})
    run = sb.run("--update")
    assert run.code == 0, run.text
    assert sb.baseline_json()["_meta"]["flags"] == flags
    assert sb.run().code == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize(
    ("umask", "expected"),
    [(0o022, 0o644), (0o002, 0o644), (0o000, 0o644), (0o077, 0o600)],
)
def test_update_writes_the_baseline_like_a_normal_checkout_file_respecting_the_umask(
    sb, umask, expected
):
    sb.mypy_counts({ALPHA: 1})
    run = sb.run("--update", "--allow-regression", umask=umask)
    assert run.code == 0, run.text
    mode = stat.S_IMODE(sb.baseline.stat().st_mode)
    assert mode == expected, f"umask {umask:03o}: baseline mode {mode:03o}, expected {expected:03o}"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_update_replaces_a_private_baseline_with_a_normal_checkout_mode(sb):
    sb.set_baseline({ALPHA: 2})
    sb.baseline.chmod(0o600)
    sb.mypy_counts({ALPHA: 1})
    run = sb.run("--update", umask=0o022)
    assert run.code == 0, run.text
    assert stat.S_IMODE(sb.baseline.stat().st_mode) == 0o644


@pytest.mark.parametrize("refused", [False, True])
def test_update_leaves_no_temporary_files_next_to_the_baseline(sb, refused):
    sb.set_baseline({ALPHA: 1})
    sb.mypy_counts({ALPHA: 2} if refused else {ALPHA: 1})
    run = sb.run("--update")
    assert run.code == (1 if refused else 0), run.text
    assert [p.name for p in sb.base.iterdir() if p.is_file()] == ["b.json"]


def test_a_note_that_quotes_an_error_line_is_not_counted_as_a_second_error(sb):
    stdout = "".join(
        [
            error_line(ALPHA, 3) + "\n",
            f'{ALPHA}:3: note: Perhaps you meant "{BETA}:7: error: something else"\n',
            summary_line(1, 1) + "\n",
        ]
    )
    sb.set_baseline({ALPHA: 1})
    sb.mypy(stdout=stdout, exit_code=1)
    run = sb.run()
    assert run.code == 0, run.text


def test_an_error_message_that_quotes_another_error_line_counts_once_for_its_own_file(sb):
    quoted = f"{ALPHA}:3: error: Cannot find implementation (see {BETA}:7: error: x)  [import]\n"
    sb.set_baseline({ALPHA: 1})
    sb.mypy(stdout=quoted + summary_line(1, 1) + "\n", exit_code=1)
    run = sb.run()
    assert run.code == 0, run.text


# --------------------------------------------------------------------------------------------
# the tracked baseline that ships with the repo
# --------------------------------------------------------------------------------------------
def _tracked(path: str) -> bool:
    done = subprocess.run(
        ["git", "ls-files", "--", path], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return bool(done.stdout.strip())


def test_the_tracked_baseline_exists_and_follows_the_contract():
    assert TRACKED_BASELINE.is_file(), "scripts/mypy_baseline.json is missing"
    assert _tracked("scripts/mypy_baseline.json"), "scripts/mypy_baseline.json is not tracked"
    data = json.loads(TRACKED_BASELINE.read_text(encoding="utf-8"))
    assert set(data) == {"_meta", "files"}
    meta = data["_meta"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", meta["mypy_version"]), meta
    assert isinstance(meta["flags"], list) and all(isinstance(f, str) for f in meta["flags"])
    assert isinstance(data["files"], dict)
    for path, count in data["files"].items():
        assert "\\" not in path and not path.startswith("/"), f"not a POSIX relative path: {path}"
        assert type(count) is int and count >= 1, f"{path}: {count!r}"
        assert (ROOT / path).is_file(), f"baseline lists a file that does not exist: {path}"


def test_the_tracked_baseline_was_captured_with_the_pinned_mypy():
    pins = [
        line.split("#", 1)[0].strip()
        for line in REQUIREMENTS_DEV.read_text(encoding="utf-8").splitlines()
    ]
    pinned = [m.group(1) for line in pins if (m := re.fullmatch(r"mypy==(\d+\.\d+\.\d+)", line))]
    assert len(pinned) == 1, f"requirements-dev.txt needs exactly one mypy==x.y.z pin: {pins}"
    meta = json.loads(TRACKED_BASELINE.read_text(encoding="utf-8"))["_meta"]
    assert meta["mypy_version"] == pinned[0], (
        f"baseline captured with mypy {meta['mypy_version']} but {pinned[0]} is pinned"
    )


# --------------------------------------------------------------------------------------------
# properties: the contract formula, checked over random baselines and reports
# --------------------------------------------------------------------------------------------
COUNTS = st.dictionaries(st.sampled_from(POOL), st.integers(min_value=1, max_value=4))
PROPERTY_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)


@PROPERTY_SETTINGS
@given(baseline=COUNTS, observed=COUNTS)
def test_property_exit_0_iff_no_file_exceeds_its_baseline_count(baseline, observed):
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Sandbox(Path(tmp))
        sandbox.set_baseline(baseline)
        sandbox.mypy_counts(observed)
        run = sandbox.run()
        regressed = sorted(f for f, n in observed.items() if n > baseline.get(f, 0))
        if regressed:
            assert_regression(run, *regressed)
        else:
            assert run.code == 0, run.text


@PROPERTY_SETTINGS
@given(baseline=COUNTS, observed=COUNTS)
def test_property_update_snapshots_the_observed_counts_and_is_then_green(baseline, observed):
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Sandbox(Path(tmp))
        sandbox.set_baseline(baseline)
        sandbox.mypy_counts(observed)
        run = sandbox.run("--update", "--allow-regression")
        assert run.code == 0, run.text
        assert sandbox.baseline_json()["files"] == observed
        assert sandbox.run().code == 0


@PROPERTY_SETTINGS
@given(baseline=COUNTS, observed=COUNTS)
def test_property_update_without_allow_regression_never_raises_a_count(baseline, observed):
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Sandbox(Path(tmp))
        sandbox.set_baseline(baseline)
        sandbox.mypy_counts(observed)
        before = sandbox.baseline_bytes()
        run = sandbox.run("--update")
        raised = any(n > baseline.get(f, 0) for f, n in observed.items())
        if raised:
            assert run.code != 0, run.text
            assert sandbox.baseline_bytes() == before
        else:
            assert run.code == 0, run.text
            assert sandbox.baseline_json()["files"] == observed
