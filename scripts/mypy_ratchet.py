#!/usr/bin/env python3
"""Per-file mypy ratchet: type errors may fall, never rise.

The repo carries legacy type errors, so mypy cannot be a plain pass/fail gate yet.
Instead every file's error count is held at or below the tracked baseline
``scripts/mypy_baseline.json``::

    {"_meta": {"mypy_version": "x.y.z", "flags": [...]},
     "files": {"src/onelot/x.py": 3, ...}}

Paths are POSIX and relative to the repo root; a file absent from ``files`` has a
baseline of 0. Counts are only comparable for the same mypy and the same flags, so a
check runs MYPY_FLAGS and treats a different mypy version, or ``_meta.flags`` that
differ from MYPY_FLAGS, as a tool error, not a pass. ``--update`` records MYPY_FLAGS.

Usage::

    .venv/bin/python scripts/mypy_ratchet.py                 # check
    .venv/bin/python scripts/mypy_ratchet.py --update        # lock in a decrease
    .venv/bin/python scripts/mypy_ratchet.py --update --allow-regression

Exit codes: 0 ok (a decrease asks you to run --update) · 1 regression (or --update
refused to raise a count) · 2 tool error: mypy missing or crashed, its version or the
flags differ from the baseline's, unparsable output, errors prevented further checking,
or a missing or invalid baseline. Exit 2 never means "probably fine".

Deliberately NOT a mypy config file: a [tool.mypy] section, mypy.ini or setup.cfg
would make the team's gate treat mypy as a hard requirement on the legacy code.
Standard library only, so it runs before anything else is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_TOOL_ERROR = 2

# Changing this needs the owner's sign-off; it must never narrow what is checked
# (tests/test_mypy_ratchet.py pins the exact tuple and denies suppressing flags).
# What mypy always runs with; --update records it in _meta.flags and a check refuses a
# baseline whose flags differ, so editing this tuple needs an --update (commit both together).
#   --config-file=      ignore any user-level or repo mypy config: same flags, same counts
#   --python-version    pin the typeshed view, so the interpreter running mypy cannot move it
MYPY_FLAGS: tuple[str, ...] = (
    "--config-file=",
    "--python-version=3.12",
    "--no-color-output",
    "--no-pretty",
    "--show-error-codes",
    "--hide-error-context",
    "src",
)
MYPY_TIMEOUT_S = 600
# The baseline is a tracked file: readable by everyone, like the rest of the checkout.
BASELINE_MODE = 0o644
# Environment variables that would change what mypy sees without changing the flags.
SCRUBBED_ENV = ("MYPYPATH",)

# The path holds no colon except a Windows drive's, so a note quoting "x.py:5: error: "
# cannot match as a second error line.
_ERROR_LINE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:]+):(?P<line>\d+)(?::\d+)?: error: ")
_SUCCESS = re.compile(r"^Success: no issues found\b", re.MULTILINE)
_FOUND = re.compile(r"^Found (?P<errors>\d+) errors? in (?P<files>\d+) files?\b", re.MULTILINE)
_VERSION = re.compile(r"^mypy (?P<version>\d+(?:\.\d+)*\S*)", re.MULTILINE)
_PREVENTED = "errors prevented further checking"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


class RatchetError(Exception):
    """A tool error: the ratchet cannot tell whether types got better or worse (exit 2)."""


@dataclass(frozen=True)
class Baseline:
    """The tracked baseline: the mypy version and flags it was captured with, and counts."""

    mypy_version: str
    flags: tuple[str, ...]
    files: dict[str, int]


@dataclass(frozen=True)
class Comparison:
    """Per-file differences between a baseline and an observed run: path -> (was, now)."""

    regressions: dict[str, tuple[int, int]]
    decreases: dict[str, tuple[int, int]]
    deleted: list[str]


def to_repo_path(raw: str, root: Path) -> str:
    """Return ``raw`` as a POSIX path relative to ``root`` (Windows separators included).

    A path outside ``root`` is returned normalised but absolute, so it is still counted.
    """
    text = posixpath.normpath(raw.strip().replace("\\", "/"))
    if text.startswith("/") or _WINDOWS_DRIVE.match(text):
        for base in {root.as_posix(), root.resolve().as_posix()}:
            prefix = base.rstrip("/") + "/"
            if text.startswith(prefix):
                return text[len(prefix) :]
    return text


def parse_version(text: str) -> str:
    """Extract ``x.y.z`` from ``mypy --version`` output, e.g. 'mypy 2.4.1 (compiled: yes)'."""
    match = _VERSION.search(text)
    if match is None:
        raise RatchetError(f"cannot read the mypy version from: {text.strip()[:200]!r}")
    return match.group("version")


def parse_mypy_output(text: str, root: Path) -> dict[str, int]:
    """Count ``path:line: error:`` lines per repo-relative POSIX path.

    The summary line is the checksum: it must be present and agree with the lines,
    otherwise the output is not understood and the result would be a guess.
    """
    if _PREVENTED in text:
        raise RatchetError(f"mypy reported that {_PREVENTED}:\n{_tail(text)}")
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        match = _ERROR_LINE.match(line)
        if match:
            counts[to_repo_path(match.group("path"), root)] += 1
    total, n_files = sum(counts.values()), len(counts)
    found = _FOUND.search(text)
    if found is not None:
        claimed = (int(found.group("errors")), int(found.group("files")))
        if claimed != (total, n_files):
            raise RatchetError(
                f"mypy summary says {claimed[0]} errors in {claimed[1]} files but "
                f"{total} error lines in {n_files} files were parsed:\n{_tail(text)}"
            )
    elif _SUCCESS.search(text) is not None:
        if total:
            raise RatchetError(f"mypy reported success but printed {total} error lines")
    else:
        raise RatchetError(f"no mypy summary line in the output:\n{_tail(text)}")
    return dict(counts)


def load_baseline(path: Path, root: Path) -> Baseline:
    """Read and validate the baseline file; any deviation from the schema is a tool error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RatchetError(f"no baseline at {path}: run --update to create it") from None
    except (OSError, ValueError) as err:
        raise RatchetError(f"unreadable baseline {path}: {err}") from err
    if not isinstance(data, dict):
        raise RatchetError(f"{path}: top level must be an object")
    meta, files = data.get("_meta"), data.get("files")
    if not isinstance(meta, dict) or not isinstance(files, dict):
        raise RatchetError(f"{path}: needs '_meta' and 'files' objects")
    version, flags = meta.get("mypy_version"), meta.get("flags")
    if not isinstance(version, str) or not version:
        raise RatchetError(f"{path}: _meta.mypy_version must be a non-empty string")
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        raise RatchetError(f"{path}: _meta.flags must be a list of strings")
    counts: dict[str, int] = {}
    for raw, count in files.items():
        # bool is an int subclass: `true` must not pass as a count of 1.
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise RatchetError(f"{path}: count for {raw!r} must be a non-negative integer")
        key = to_repo_path(raw, root)
        if key in counts:
            raise RatchetError(f"{path}: {raw!r} duplicates {key!r} once normalised to POSIX")
        counts[key] = count
    return Baseline(mypy_version=version, flags=tuple(flags), files=counts)


def compare(baseline: Mapping[str, int], observed: Mapping[str, int], root: Path) -> Comparison:
    """Compare counts file by file; a file missing from either side counts as 0."""
    regressions: dict[str, tuple[int, int]] = {}
    decreases: dict[str, tuple[int, int]] = {}
    for path in sorted(set(baseline) | set(observed)):
        was, now = baseline.get(path, 0), observed.get(path, 0)
        if now > was:
            regressions[path] = (was, now)
        elif now < was:
            decreases[path] = (was, now)
    deleted = sorted(p for p in baseline if not (root / p).exists())
    return Comparison(regressions=regressions, decreases=decreases, deleted=deleted)


def run_mypy(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Run ``python -m mypy *args`` with this interpreter, in ``root``.

    mypy is told to write UTF-8 (a Windows console would otherwise give cp1252), and
    any byte that still is not UTF-8 is replaced rather than crashing the ratchet: the
    counts come from whole lines and the summary checksum, not from those bytes.
    """
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "mypy", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=root, env=env, capture_output=True, timeout=MYPY_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise RatchetError(f"could not run {' '.join(cmd)}: {err}") from err
    return subprocess.CompletedProcess(
        cmd, proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    )


def installed_mypy_version(root: Path) -> str:
    """The version of mypy that ``python -m mypy`` resolves to for this interpreter."""
    proc = run_mypy(["--version"], root)
    if proc.returncode != 0:
        raise RatchetError(
            f"mypy is not runnable with {sys.executable} (exit {proc.returncode}); install the "
            f"pinned version from requirements-dev.txt:\n{_tail(proc.stdout + proc.stderr)}"
        )
    return parse_version(proc.stdout)


def observe(root: Path, flags: Sequence[str]) -> dict[str, int]:
    """Run mypy with ``flags`` and return per-file error counts."""
    proc = run_mypy(flags, root)
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode not in (0, 1):
        raise RatchetError(
            f"mypy exited {proc.returncode} (crash or usage error):\n{_tail(output)}"
        )
    counts = parse_mypy_output(output, root)
    if (proc.returncode == 1) != bool(counts):
        raise RatchetError(
            f"mypy exit {proc.returncode} disagrees with {sum(counts.values())} errors"
        )
    return counts


def write_baseline(path: Path, version: str, counts: Mapping[str, int]) -> None:
    """Write the baseline atomically: sorted, zero counts dropped, LF line endings."""
    payload = {
        "_meta": {"mypy_version": version, "flags": list(MYPY_FLAGS)},
        "files": {p: counts[p] for p in sorted(counts) if counts[p] > 0},
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    except OSError as err:
        raise RatchetError(f"cannot write baseline {path}: {err}") from err
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, BASELINE_MODE & ~_current_umask())  # mkstemp made it 0600
        os.replace(tmp, path)
    except BaseException as err:
        Path(tmp).unlink(missing_ok=True)
        if isinstance(err, OSError):
            raise RatchetError(f"cannot write baseline {path}: {err}") from err
        raise


def check(root: Path, baseline_path: Path) -> int:
    """Compare a fresh mypy run with the baseline; never writes."""
    baseline = load_baseline(baseline_path, root)
    if baseline.flags != MYPY_FLAGS:
        # Running the recorded flags would let an edit to the JSON narrow the target or
        # add a suppressing flag, and every count would then "fall" to a green check.
        raise RatchetError(
            f"{baseline_path} _meta.flags {list(baseline.flags)} differ from this script's "
            f"MYPY_FLAGS {list(MYPY_FLAGS)}: counts are only comparable under the same flags. "
            "If MYPY_FLAGS changed on purpose, run --update and commit both files together."
        )
    version = installed_mypy_version(root)
    if version != baseline.mypy_version:
        raise RatchetError(
            f"baseline was captured with mypy {baseline.mypy_version} but {version} is "
            "installed: install the pinned mypy, or re-baseline with --update"
        )
    observed = observe(root, MYPY_FLAGS)
    result = compare(baseline.files, observed, root)
    for path in result.deleted:
        _err(f"warning: baseline entry for deleted file {path}; run --update to drop it")
    if result.regressions:
        print(f"mypy ratchet: type errors rose in {len(result.regressions)} file(s):")
        _print_changes(result.regressions)
        return EXIT_REGRESSION
    if result.decreases:
        print(f"mypy ratchet: type errors fell in {len(result.decreases)} file(s):")
        _print_changes(result.decreases)
        print("run --update to lock in the improvement")
        return EXIT_OK
    print(f"mypy ratchet: OK, {sum(observed.values())} errors, none above the baseline")
    return EXIT_OK


def update(root: Path, baseline_path: Path, allow_regression: bool) -> int:
    """Rewrite the baseline from a fresh run; refuses to raise a count unless allowed.

    A missing baseline counts as an empty one, so deleting the file cannot launder new
    errors: bootstrapping a baseline that holds any error needs --allow-regression.
    """
    previous_files = load_baseline(baseline_path, root).files if baseline_path.exists() else {}
    version = installed_mypy_version(root)
    observed = observe(root, MYPY_FLAGS)
    result = compare(previous_files, observed, root)
    if result.regressions and not allow_regression:
        print(
            f"mypy ratchet: refusing to raise the baseline for {len(result.regressions)} file(s):"
        )
        _print_changes(result.regressions)
        print("fix them, or pass --allow-regression to accept them deliberately")
        return EXIT_REGRESSION
    write_baseline(baseline_path, version, observed)
    print(
        f"mypy ratchet: baseline written: {sum(observed.values())} errors in "
        f"{sum(1 for n in observed.values() if n)} files (mypy {version}) -> {baseline_path}"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--update", action="store_true", help="rewrite the baseline")
    parser.add_argument(
        "--allow-regression", action="store_true", help="with --update: allow counts to rise"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1], help="repo root"
    )
    parser.add_argument("--baseline", type=Path, help="default: <root>/scripts/mypy_baseline.json")
    args = parser.parse_args(argv)
    if args.allow_regression and not args.update:
        parser.error("--allow-regression only applies with --update")
    root: Path = args.root.resolve()
    baseline_path: Path = args.baseline or root / "scripts" / "mypy_baseline.json"
    try:
        if args.update:
            return update(root, baseline_path, args.allow_regression)
        return check(root, baseline_path)
    except RatchetError as err:
        _err(f"mypy ratchet: tool error: {err}")
        return EXIT_TOOL_ERROR
    except Exception as err:  # noqa: BLE001 - an uncaught exception would exit 1 = "regression"
        _err(f"mypy ratchet: internal error (a bug in the ratchet): {type(err).__name__}: {err}")
        return EXIT_TOOL_ERROR


def _print_changes(changes: Mapping[str, tuple[int, int]]) -> None:
    for path, (was, now) in changes.items():
        print(f"  {path}: {was} -> {now}")


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _current_umask() -> int:
    """The process umask (reading it means setting it, so it is put straight back)."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _decode(raw: bytes) -> str:
    """UTF-8 with universal newlines; undecodable bytes become U+FFFD, with a note."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        _err("note: mypy output was not valid UTF-8; undecodable bytes were replaced")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


if __name__ == "__main__":
    sys.exit(main())
