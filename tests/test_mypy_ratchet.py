"""scripts/mypy_ratchet.py: the per-file type-error ratchet (US-000 AC3).

The real mypy never runs here. A fake ``mypy`` package goes first on the PYTHONPATH of
the subprocess the ratchet starts, and each test scripts its version, output and exit
code. Most tests call ``main()`` in-process (one child process per mypy call); the CLI
class runs the script itself to prove the real exit codes.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mypy_ratchet.py"
VERSION = "1.2.3"

FAKE_INIT = """\
import json, os, pathlib
_BEHAVIOUR = json.loads(pathlib.Path(os.environ["FAKE_MYPY_BEHAVIOUR"]).read_text("utf-8"))
if _BEHAVIOUR["missing"]:
    raise ModuleNotFoundError("No module named 'mypy'")
"""
FAKE_MAIN = """\
import json, os, sys
from mypy import _BEHAVIOUR
with open(os.environ["FAKE_MYPY_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                         "python": sys.executable,
                         "mypypath": os.environ.get("MYPYPATH")}) + "\\n")
if "--version" in sys.argv[1:]:
    print("mypy " + _BEHAVIOUR["version"])
    sys.exit(0)
sys.stdout.write(_BEHAVIOUR["stdout"])
sys.stderr.write(_BEHAVIOUR["stderr"])
sys.exit(_BEHAVIOUR["rc"])
"""


def mypy_output(errors: dict[str, int], checked: int = 7) -> str:
    """What mypy prints for ``errors`` (path -> count), notes included."""
    lines: list[str] = []
    for path, count in errors.items():
        for i in range(count):
            lines.append(f'{path}:{i + 1}: error: Name "x{i}" is not defined  [name-defined]')
        lines.append(f"{path}:1: note: See https://mypy.rtfd.io/en/stable/_refs.html")
    total = sum(errors.values())
    files = sum(1 for n in errors.values() if n)
    if total == 0:
        lines.append(f"Success: no issues found in {checked} source files")
    else:
        plural_e = "s" if total != 1 else ""
        plural_f = "s" if files != 1 else ""
        lines.append(
            f"Found {total} error{plural_e} in {files} file{plural_f} (checked {checked} source files)"
        )
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mypy_ratchet", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


@dataclass
class Rig:
    """A scratch repository plus the scripted fake mypy that the ratchet will start."""

    root: Path
    fake_dir: Path
    behaviour: Path
    log: Path
    flags: list[str]

    @property
    def baseline(self) -> Path:
        return self.root / "scripts" / "mypy_baseline.json"

    def mypy(
        self,
        stdout: str = "",
        *,
        rc: int | None = None,
        stderr: str = "",
        version: str = VERSION,
        missing: bool = False,
    ) -> None:
        if rc is None:
            rc = 1 if ": error: " in stdout else 0
        payload = {
            "stdout": stdout,
            "stderr": stderr,
            "rc": rc,
            "version": version,
            "missing": missing,
        }
        self.behaviour.write_text(json.dumps(payload), encoding="utf-8")

    def sources(self, *rels: str) -> None:
        for rel in rels:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")

    def write_baseline(
        self, files: dict[str, int], *, version: str = VERSION, flags: list[str] | None = None
    ) -> None:
        meta = {"mypy_version": version, "flags": self.flags if flags is None else flags}
        self.baseline.write_text(json.dumps({"_meta": meta, "files": files}), encoding="utf-8")

    def read_baseline(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.baseline.read_text(encoding="utf-8"))
        return data

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def env(self) -> dict[str, str]:
        return {
            "PYTHONPATH": str(self.fake_dir),
            "FAKE_MYPY_BEHAVIOUR": str(self.behaviour),
            "FAKE_MYPY_LOG": str(self.log),
        }


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ratchet: ModuleType) -> Iterator[Rig]:
    fake_dir = tmp_path / "fake"
    (fake_dir / "mypy").mkdir(parents=True)
    (fake_dir / "mypy" / "__init__.py").write_text(FAKE_INIT, encoding="utf-8")
    (fake_dir / "mypy" / "__main__.py").write_text(FAKE_MAIN, encoding="utf-8")
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    rig = Rig(
        root=root,
        fake_dir=fake_dir,
        behaviour=tmp_path / "behaviour.json",
        log=tmp_path / "calls.jsonl",
        flags=list(ratchet.MYPY_FLAGS),
    )
    rig.sources("src/pkg/a.py", "src/pkg/b.py", "src/pkg/c.py")
    rig.mypy(mypy_output({}))
    for key, value in rig.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MYPYPATH", raising=False)
    yield rig


def run(
    ratchet: ModuleType, rig: Rig, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    code = ratchet.main(["--root", str(rig.root), *args])
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- parsing
class TestParseMypyOutput:
    def test_counts_errors_per_file_and_ignores_notes(self, ratchet: ModuleType, tmp_path: Path):
        text = mypy_output({"src/pkg/a.py": 2, "src/pkg/b.py": 1})
        assert ratchet.parse_mypy_output(text, tmp_path) == {"src/pkg/a.py": 2, "src/pkg/b.py": 1}

    def test_success_summary_means_no_errors(self, ratchet: ModuleType, tmp_path: Path):
        assert ratchet.parse_mypy_output(mypy_output({}), tmp_path) == {}

    def test_windows_absolute_and_dotted_paths_become_posix_and_repo_relative(
        self, ratchet: ModuleType, tmp_path: Path
    ):
        text = (
            "src\\pkg\\a.py:3: error: boom  [misc]\n"
            f"{tmp_path.as_posix()}/src/pkg/b.py:4: error: boom  [misc]\n"
            "./src/pkg/../pkg/c.py:5:7: error: with a column  [misc]\n"
            "Found 3 errors in 3 files (checked 3 source files)\n"
        )
        assert ratchet.parse_mypy_output(text, tmp_path) == {
            "src/pkg/a.py": 1,
            "src/pkg/b.py": 1,
            "src/pkg/c.py": 1,
        }

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("", id="empty"),
            pytest.param("src/pkg/a.py:1: error: boom  [misc]\n", id="no-summary"),
            pytest.param("Traceback (most recent call last):\nKeyError: 'x'\n", id="traceback"),
            pytest.param(
                "src/pkg/a.py:1: error: boom  [misc]\nFound 3 errors in 1 file (checked 1 source file)\n",
                id="error-total-disagrees",
            ),
            pytest.param(
                "src/pkg/a.py:1: error: boom  [misc]\nFound 1 error in 2 files (checked 2 source files)\n",
                id="file-total-disagrees",
            ),
            pytest.param(
                "src/pkg/a.py:1: error: boom  [misc]\nSuccess: no issues found in 1 source file\n",
                id="success-with-errors",
            ),
            pytest.param(
                "src: error: Cannot read file\n"
                "Found 1 error in 1 file (errors prevented further checking)\n",
                id="prevented-further-checking",
            ),
        ],
    )
    def test_unparsable_or_inconsistent_output_is_a_tool_error(
        self, ratchet: ModuleType, tmp_path: Path, text: str
    ):
        with pytest.raises(ratchet.RatchetError):
            ratchet.parse_mypy_output(text, tmp_path)

    def test_version_is_read_from_the_banner(self, ratchet: ModuleType):
        assert ratchet.parse_version("mypy 2.4.1 (compiled: yes)\n") == "2.4.1"
        assert ratchet.parse_version("mypy 1.2.3\n") == "1.2.3"
        with pytest.raises(ratchet.RatchetError):
            ratchet.parse_version("No module named mypy\n")


# --------------------------------------------------------------------------- baseline file
class TestBaselineFile:
    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("{not json", id="not-json"),
            pytest.param("[]", id="not-an-object"),
            pytest.param('{"files": {}}', id="no-meta"),
            pytest.param('{"_meta": {"flags": []}, "files": {}}', id="no-version"),
            pytest.param(
                '{"_meta": {"mypy_version": "1", "flags": "src"}, "files": {}}', id="flags-str"
            ),
            pytest.param('{"_meta": {"mypy_version": "1", "flags": []}}', id="no-files"),
            pytest.param(
                '{"_meta": {"mypy_version": "1", "flags": []}, "files": {"a.py": -1}}',
                id="negative",
            ),
            pytest.param(
                '{"_meta": {"mypy_version": "1", "flags": []}, "files": {"a.py": true}}', id="bool"
            ),
            pytest.param(
                '{"_meta": {"mypy_version": "1", "flags": []}, "files": {"a.py": 1.5}}', id="float"
            ),
            pytest.param(
                '{"_meta": {"mypy_version": "1", "flags": []}, "files": {"a.py": "2"}}', id="string"
            ),
            pytest.param(
                '{"_meta": {"mypy_version": "1", "flags": []}, "files": {"a\\\\b.py": 1, "a/b.py": 2}}',
                id="duplicate-after-normalising",
            ),
        ],
    )
    def test_an_invalid_baseline_is_a_tool_error(
        self, ratchet: ModuleType, tmp_path: Path, content: str
    ):
        path = tmp_path / "baseline.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ratchet.RatchetError):
            ratchet.load_baseline(path, tmp_path)

    def test_baseline_keys_are_normalised_to_posix(self, ratchet: ModuleType, tmp_path: Path):
        path = tmp_path / "baseline.json"
        meta = {"mypy_version": "1", "flags": ["src"]}
        path.write_text(json.dumps({"_meta": meta, "files": {"src\\pkg\\a.py": 2}}), "utf-8")
        baseline = ratchet.load_baseline(path, tmp_path)
        assert baseline.files == {"src/pkg/a.py": 2}
        assert baseline.mypy_version == "1"
        assert baseline.flags == ("src",)


# --------------------------------------------------------------------------- check mode
class TestCheck:
    def test_unchanged_counts_pass(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 2})
        rig.mypy(mypy_output({"src/pkg/a.py": 2}))
        code, out, _ = run(ratchet, rig, capsys)
        assert code == 0
        assert "--update" not in out

    def test_a_new_error_in_a_baselined_file_is_a_regression(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 2, "src/pkg/b.py": 1})
        rig.mypy(mypy_output({"src/pkg/a.py": 3, "src/pkg/b.py": 1}))
        code, out, _ = run(ratchet, rig, capsys)
        assert code == 1
        assert "src/pkg/a.py" in out
        assert "src/pkg/b.py" not in out

    def test_errors_in_a_file_absent_from_the_baseline_count_from_zero(
        self, ratchet, rig: Rig, capsys
    ):
        rig.write_baseline({"src/pkg/a.py": 5})
        rig.mypy(mypy_output({"src/pkg/a.py": 1, "src/pkg/c.py": 1}))  # total still falls
        code, out, _ = run(ratchet, rig, capsys)
        assert code == 1
        assert "src/pkg/c.py" in out

    def test_a_decrease_passes_and_asks_for_update(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 3})
        rig.mypy(mypy_output({"src/pkg/a.py": 1}))
        code, out, _ = run(ratchet, rig, capsys)
        assert code == 0
        assert "run --update" in out
        assert rig.read_baseline()["files"] == {"src/pkg/a.py": 3}, "check mode never writes"

    def test_a_baseline_entry_for_a_deleted_file_warns(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/gone.py": 2})
        code, out, err = run(ratchet, rig, capsys)
        assert code == 0
        assert "src/pkg/gone.py" in err
        assert "warning" in err.lower()

    def test_windows_paths_from_mypy_match_posix_baseline_keys(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 1})
        rig.mypy(mypy_output({"src\\pkg\\a.py": 1}))
        code, _, _ = run(ratchet, rig, capsys)
        assert code == 0

    def test_mypy_runs_as_a_module_of_this_interpreter_in_root_with_the_recorded_flags(
        self, ratchet, rig: Rig, capsys, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MYPYPATH", "/somewhere/else")
        rig.write_baseline({})
        code, _, _ = run(ratchet, rig, capsys)
        assert code == 0
        version_call, check_call = rig.calls()
        assert version_call["argv"] == ["--version"]
        assert check_call["argv"] == rig.flags
        assert Path(check_call["cwd"]).resolve() == rig.root.resolve()
        assert Path(check_call["python"]).resolve() == Path(sys.executable).resolve()
        assert check_call["mypypath"] is None, "a developer's MYPYPATH must not skew the counts"

    def test_flags_include_the_source_tree_and_no_config_file(self, ratchet):
        assert "src" in ratchet.MYPY_FLAGS
        assert "--config-file=" in ratchet.MYPY_FLAGS

    def test_the_default_baseline_lives_in_scripts_under_root(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 1})
        rig.mypy(mypy_output({"src/pkg/a.py": 2}))
        code, _, _ = run(ratchet, rig, capsys)
        assert code == 1

    def test_an_explicit_baseline_path_is_honoured(self, ratchet, rig: Rig, capsys, tmp_path):
        other = tmp_path / "elsewhere.json"
        rig.write_baseline({"src/pkg/a.py": 5})
        other.write_text(
            json.dumps({"_meta": {"mypy_version": VERSION, "flags": rig.flags}, "files": {}}),
            encoding="utf-8",
        )
        rig.mypy(mypy_output({"src/pkg/a.py": 1}))
        code, _, _ = run(ratchet, rig, capsys, "--baseline", str(other))
        assert code == 1

    @pytest.mark.parametrize(
        "behaviour",
        [
            pytest.param({"missing": True}, id="mypy-missing"),
            pytest.param(
                {"version": "9.9.9", "stdout": mypy_output({"src/pkg/a.py": 1})},
                id="version-mismatch",
            ),
            pytest.param(
                {
                    "stdout": "",
                    "stderr": "Traceback (most recent call last):\nINTERNAL ERROR\n",
                    "rc": 2,
                },
                id="crash",
            ),
            pytest.param({"stdout": "garbage\n", "rc": 0}, id="unparsable-rc0"),
            pytest.param({"stdout": "garbage\n", "rc": 1}, id="unparsable-rc1"),
            pytest.param(
                {
                    "stdout": "src/pkg/a.py:1: error: boom  [misc]\n",
                    "stderr": "Found 1 error in 1 file (errors prevented further checking)\n",
                    "rc": 2,
                },
                id="prevented-further-checking",
            ),
            pytest.param({"stdout": mypy_output({}), "rc": 2}, id="success-text-but-rc2"),
        ],
    )
    def test_tool_errors_exit_2_and_never_0(self, ratchet, rig: Rig, capsys, behaviour):
        rig.write_baseline({"src/pkg/a.py": 99})
        rig.mypy(**behaviour)
        code, _, err = run(ratchet, rig, capsys)
        assert code == 2
        assert err.strip(), "a tool error must say what went wrong"

    def test_a_flag_change_since_the_baseline_is_a_tool_error(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({}, flags=["--strict", "src"])
        code, _, err = run(ratchet, rig, capsys)
        assert code == 2
        assert "--update" in err

    def test_a_missing_baseline_is_a_tool_error(self, ratchet, rig: Rig, capsys):
        code, _, err = run(ratchet, rig, capsys)
        assert code == 2
        assert "--update" in err

    def test_allow_regression_without_update_is_a_usage_error(self, ratchet, rig: Rig):
        with pytest.raises(SystemExit) as exc:
            ratchet.main(["--root", str(rig.root), "--allow-regression"])
        assert exc.value.code == 2


# --------------------------------------------------------------------------- --update
class TestUpdate:
    def test_update_creates_a_missing_baseline(self, ratchet, rig: Rig, capsys):
        rig.mypy(mypy_output({"src\\pkg\\b.py": 1, "src/pkg/a.py": 2}))
        code, _, _ = run(ratchet, rig, capsys, "--update")
        assert code == 0
        assert rig.read_baseline() == {
            "_meta": {"mypy_version": VERSION, "flags": rig.flags},
            "files": {"src/pkg/a.py": 2, "src/pkg/b.py": 1},
        }
        text = rig.baseline.read_text(encoding="utf-8")
        assert text.endswith("\n") and "\r" not in text
        assert list(json.loads(text)["files"]) == ["src/pkg/a.py", "src/pkg/b.py"], "sorted"

    def test_update_locks_in_a_decrease_and_drops_clean_and_deleted_files(
        self, ratchet, rig: Rig, capsys
    ):
        rig.write_baseline({"src/pkg/a.py": 3, "src/pkg/b.py": 1, "src/pkg/gone.py": 4})
        rig.mypy(mypy_output({"src/pkg/a.py": 1}))
        code, _, _ = run(ratchet, rig, capsys, "--update")
        assert code == 0
        assert rig.read_baseline()["files"] == {"src/pkg/a.py": 1}

    def test_update_records_a_new_mypy_version(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 1}, version="0.0.1")
        rig.mypy(mypy_output({"src/pkg/a.py": 1}), version="2.0.0")
        code, _, _ = run(ratchet, rig, capsys, "--update")
        assert code == 0
        assert rig.read_baseline()["_meta"]["mypy_version"] == "2.0.0"

    @pytest.mark.parametrize(
        "observed",
        [
            pytest.param({"src/pkg/a.py": 2}, id="raise-a-count"),
            pytest.param({"src/pkg/a.py": 1, "src/pkg/c.py": 1}, id="new-file"),
        ],
    )
    def test_update_refuses_to_raise_any_count(self, ratchet, rig: Rig, capsys, observed):
        rig.write_baseline({"src/pkg/a.py": 1})
        before = rig.baseline.read_bytes()
        rig.mypy(mypy_output(observed))
        code, out, _ = run(ratchet, rig, capsys, "--update")
        assert code == 1
        assert "--allow-regression" in out
        assert rig.baseline.read_bytes() == before

    def test_allow_regression_lets_update_raise_a_count(self, ratchet, rig: Rig, capsys):
        rig.write_baseline({"src/pkg/a.py": 1})
        rig.mypy(mypy_output({"src/pkg/a.py": 2, "src/pkg/c.py": 1}))
        code, _, _ = run(ratchet, rig, capsys, "--update", "--allow-regression")
        assert code == 0
        assert rig.read_baseline()["files"] == {"src/pkg/a.py": 2, "src/pkg/c.py": 1}

    def test_a_tool_error_during_update_leaves_the_baseline_untouched(
        self, ratchet, rig: Rig, capsys
    ):
        rig.write_baseline({"src/pkg/a.py": 1})
        before = rig.baseline.read_bytes()
        rig.mypy("garbage\n", rc=1)
        code, _, _ = run(ratchet, rig, capsys, "--update", "--allow-regression")
        assert code == 2
        assert rig.baseline.read_bytes() == before


# --------------------------------------------------------------------------- the real CLI
class TestCli:
    @pytest.mark.parametrize(
        ("observed", "rc", "expected"),
        [
            pytest.param({"src/pkg/a.py": 1}, None, 0, id="ok"),
            pytest.param({"src/pkg/a.py": 2}, None, 1, id="regression"),
            pytest.param({}, 2, 2, id="tool-error"),
        ],
    )
    def test_exit_codes(self, rig: Rig, observed, rc, expected):
        rig.write_baseline({"src/pkg/a.py": 1})
        rig.mypy(mypy_output(observed), rc=rc)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(rig.root)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == expected, proc.stdout + proc.stderr
