"""Packaging and launch regressions (US-082).

* the shell start scripts pass `-m onelot run --port N ...` (once they did not say `run`,
  so the app could not start at all);
* `python -m onelot --port N` with no command word still means `run`;
* pyproject.toml and requirements.txt list the same dependencies, and the lock file pins them;
* the Windows stop script expands its variable at run time;
* README and QUICKSTART keep the house prose conventions.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


def _requirements(path: Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(re.sub(r"\s+", "", line))
    return out


def _pyproject_dependencies() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies = \[(.*?)^\]", text, re.S | re.M)
    assert block, "pyproject.toml has no dependencies list"
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', block.group(1))
    return [re.sub(r"\s+", "", i.replace('\\"', '"')) for i in items]


def _name(requirement: str) -> str:
    return re.split(r"[\[<>=!~;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def test_pyproject_dependencies_equal_requirements_txt() -> None:
    assert sorted(_pyproject_dependencies()) == sorted(_requirements(ROOT / "requirements.txt"))


def test_pyproject_declares_the_onelot_console_script() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'onelot = "onelot.__main__:main"' in text


def test_lock_file_pins_every_requirement() -> None:
    lock = _requirements(ROOT / "requirements.lock.txt")
    pinned = {_name(x) for x in lock if "==" in x}
    for req in _requirements(ROOT / "requirements.txt"):
        if "win32" in req:
            continue  # Windows-only packages are listed unpinned at the end of the lock file
        assert _name(req) in pinned, f"{req} is not pinned in requirements.lock.txt"
    assert not any(x.startswith("-e") or "file:" in x or "@" in x for x in lock)


def test_dockerfile_installs_from_the_lock_and_runs_the_desk() -> None:
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock.txt" in docker and "EXPOSE 8787" in docker
    assert "USER onelot" in docker
    assert '"-m", "onelot", "run", "--host", "0.0.0.0", "--port", "8787"' in docker
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for must in (".env", "data/", "logs/", ".venv/", ".git/"):
        assert must in ignored
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8787:8787"' in compose and "./data:/app/data" in compose


@pytest.mark.parametrize("script", ["start_mac.sh", "start_linux.sh"])
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ([], ["-m", "onelot", "run", "--port", "8787"]),
        (["--mock", "--port", "8799"], ["-m", "onelot", "run", "--port", "8799", "--mock"]),
        (["--lan"], ["-m", "onelot", "run", "--port", "8787", "--lan"]),
    ],
)
@pytest.mark.skipif(BASH is None, reason="bash is needed")
def test_start_scripts_pass_the_run_command(
    tmp_path: Path, script: str, given: list[str], expected: list[str]
) -> None:
    app = tmp_path / "app dir"  # a space on purpose: the owner's own folder has them
    (app / "scripts").mkdir(parents=True)
    for name in ("start_mac.sh", "start_linux.sh"):
        shutil.copy(ROOT / "scripts" / name, app / "scripts" / name)
    log = tmp_path / "args.txt"
    stub_py = app / ".venv" / "bin" / "python"
    stub_py.parent.mkdir(parents=True)
    stub_py.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > "$STUB_LOG"\n')
    stub_py.chmod(stub_py.stat().st_mode | stat.S_IEXEC)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py3 = bindir / "python3"
    py3.write_text("#!/bin/sh\necho 1\n")  # answers the script's version check
    py3.chmod(py3.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "STUB_LOG": str(log), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    env.pop("ONELOT_PORT", None)
    proc = subprocess.run(
        [BASH, str(app / "scripts" / script), *given],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().split() == expected


def test_a_bare_port_flag_means_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from onelot import __main__ as cli

    seen: dict[str, object] = {}

    def fake_run(args, cfg, secrets) -> int:
        seen["port"], seen["mock"] = args.port, args.mock
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    monkeypatch.setattr(cli, "setup_logging", lambda **_: None)
    assert cli.main(["--port", "8796", "--mock"]) == 0
    assert seen == {"port": 8796, "mock": True}


def test_every_cli_command_word_is_known_to_the_run_default() -> None:
    from onelot import __main__ as cli

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")  # noqa: SLF001
    src = Path(cli.__file__).read_text(encoding="utf-8")
    for word in sub.choices:
        assert f'"{word}"' in src.split("known = {")[1].split("}")[0], word


def test_windows_stop_script_expands_its_variable_at_run_time() -> None:
    bat = (ROOT / "scripts" / "stop_win.bat").read_text(encoding="utf-8")
    assert "EnableDelayedExpansion" in bat and "!ENGINEPID!" in bat


def test_stop_and_start_scripts_agree_on_the_pid_file() -> None:
    lock = (ROOT / "src" / "onelot" / "engine.py").read_text(encoding="utf-8")
    assert '"engine.pid"' in lock
    assert "data/engine.pid" in (ROOT / "scripts" / "stop_mac.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["README.md", "QUICKSTART.md"])
def test_prose_conventions(name: str) -> None:
    fenced = False
    for n, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            fenced = not fenced
            continue
        assert "—" not in line and "–" not in line, f"{name}:{n} has a dash"
        if fenced or line.startswith("|"):
            continue
        assert len(line) <= 100, f"{name}:{n} is {len(line)} characters"
