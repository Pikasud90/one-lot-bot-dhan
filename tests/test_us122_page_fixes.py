"""US-122 main page fixes (developer tests): India-time stamps, plain words, build check."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "onelot" / "ui" / "app.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def js(names: list[str], expression: str) -> Any:
    """Run `expression` in node after the one-line `const name = ...;` helpers of app.js."""
    source = APP_JS.read_text(encoding="utf-8")
    lines = []
    for name in names:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
        assert found, name
        lines.append(found.group(0))
    lines.append(f"console.log(JSON.stringify({expression}));")
    done = subprocess.run(
        ["node", "-e", "\n".join(lines)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a1_tsf_converts_zoned_times_to_india_time() -> None:
    out = js(
        ["tsf"],
        '[tsf("2026-11-25T03:45:00+00:00"), tsf("2026-11-25T09:15:00+05:30"),'
        ' tsf("2026-11-25T09:15:00"), tsf("2026-11-25T03:45:00Z"), tsf(null), tsf("")]',
    )
    assert out == [
        "2026-11-25 09:15",
        "2026-11-25 09:15",
        "2026-11-25 09:15",
        "2026-11-25 09:15",
        "–",
        "–",
    ]


@needs_node
def test_a1_tsf_unzoned_text_is_cut_as_before() -> None:
    assert js(["tsf"], 'tsf("2026-11-25T20:05:59")') == "2026-11-25 20:05"
