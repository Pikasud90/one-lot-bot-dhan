"""The alert confidence the web app shows is floored like the phone's (US-003 rule; US-030 UX).

`notify.base.shown_confidence` (US-003) floors the score and clamps it to 0..100, so a shown
number never crosses a label threshold (high >= 65, low < 45): 64.9 shows 64, never a
"medium 65". app.js used `toFixed(0)`, which rounds 64.9 up to 65. `confShown(score)` is
the same rule in JS (null for a missing, non-numeric or non-finite score) and every place
the app prints an alert's confidence goes through it.

No browser: the helpers are lifted out of app.js and run in node. Skipped without node.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"
SOURCE = APP_JS.read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def one_line(name: str) -> str:
    """The single-line definition of `name` in app.js (`const name = ...;` or a function)."""
    found = re.search(rf"^(?:const {name} = .*;|function {name}\(.*\}})$", SOURCE, re.M)
    assert found, f"{name} is not defined on one line in {APP_JS}"
    return found.group(0)


def js(names: list[str], expression: str, payload: Any) -> Any:
    script = "\n".join(
        [*(one_line(n) for n in names), "const P = JSON.parse(process.argv[1]);"]
        + [f"console.log(JSON.stringify({expression}));"]
    )
    done = subprocess.run(
        ["node", "-e", script, "--", json.dumps(payload)],  # "--": a "-3" is data, not a flag
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def python_rule(score: object) -> int | None:
    """notify.base.shown_confidence (US-003), restated: the oracle for the JS helper."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if isinstance(score, float) and not math.isfinite(score):
        return None
    return 100 if score >= 100 else 0 if score <= 0 else math.floor(score)


CASES = [64.9, 65, 65.0001, 44.99, 45, 0.4, 0, -3, 99.999, 100, 100.5, 150, 62.5, 1e308]


class TestConfShown:
    @pytest.mark.parametrize("score", CASES)
    def test_floors_and_clamps_like_the_phone(self, score):
        assert js(["confShown"], "confShown(P)", score) == python_rule(score)

    def test_the_us003_example(self):
        assert js(["confShown"], "confShown(P)", 64.9) == 64

    @pytest.mark.parametrize(
        "expression",
        ["confShown(null)", "confShown(undefined)", "confShown(NaN)", "confShown(Infinity)"]
        + ["confShown(-Infinity)", 'confShown("64")', "confShown(true)", "confShown({})"],
    )
    def test_nothing_usable_is_null(self, expression):
        assert js(["confShown"], expression, None) is None


class TestTheAppShowsTheFlooredNumber:
    def test_the_alert_badge(self):
        payload = {"score": 64.9, "label": "medium", "parts": {}}
        html = js(["esc", "fmt", "confShown", "confBadge"], "confBadge(P)", payload)
        assert ">64 medium<" in html

    def test_a_badge_without_a_score_shows_a_dash(self):
        payload = {"label": "medium", "parts": {}}
        html = js(["esc", "fmt", "confShown", "confBadge"], "confBadge(P)", payload)
        assert ">– medium<" in html

    def test_no_confidence_score_is_rounded_anywhere(self):
        # Every alert confidence goes through confShown: none is formatted straight to 0 dp.
        assert not re.search(r"fmt\(c\.score, 0\)", SOURCE)
        assert not re.search(r"confidence \|\| \{\}\)\.score, 0\)", SOURCE)
        assert len(re.findall(r"confShown\(", SOURCE)) >= 3  # badge, alert detail, focus
