"""US-028 UI: the Desk header text names the delayed instruments (src/onelot/ui/app.js).

`delayNote(source)` drives both the header badge and the desk-source line. Manager decision:
"delayed ~15 min: A, B, C +N" (first three keys, then a count) when `source.delayed_keys` is
non-empty, "live" when it is empty, and "delayed ~15 min" when `delayed_keys` is missing (an
older snapshot) but `delayed` is true. The helper is plain text on purpose: `bd()` escapes it
exactly once, so a key such as "M&M" must not be escaped twice.

No browser and no JS harness: the one-line helpers are lifted out of app.js and evaluated with
node. Skipped (with a reason) on a machine without node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def run_app_js(helpers: list[str], expression: str, payload) -> object:
    """Evaluate `expression` (`P` is the JSON payload) after loading the named one-line
    `const name = ...;` helpers from app.js."""
    js = APP_JS.read_text(encoding="utf-8")
    lines = []
    for name in helpers:
        m = re.search(rf"^const {name} = .*;$", js, re.M)
        assert m, f"`const {name} = ...;` not found as a single line in {APP_JS}"
        lines.append(m.group(0))
    script = "\n".join([*lines, "const P = JSON.parse(process.argv[1]);", expression])
    done = subprocess.run(
        ["node", "-e", script, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def delay_note(source) -> str:
    out = run_app_js(["delayNote"], "console.log(JSON.stringify(delayNote(P)))", source)
    assert isinstance(out, str)
    return out


class TestDelayNote:
    @pytest.mark.parametrize(
        "source, text",
        [
            ({"delayed": False, "delayed_keys": []}, "live"),
            ({"delayed": True, "delayed_keys": ["BANKNIFTY"]}, "delayed ~15 min: BANKNIFTY"),
            (
                {"delayed": True, "delayed_keys": ["A", "B", "C"]},
                "delayed ~15 min: A, B, C",
            ),
            (
                {"delayed": True, "delayed_keys": ["A", "B", "C", "D"]},
                "delayed ~15 min: A, B, C +1",
            ),
            (
                {"delayed": True, "delayed_keys": [f"K{i}" for i in range(12)]},
                "delayed ~15 min: K0, K1, K2 +9",
            ),
        ],
        ids=["empty-is-live", "one-key", "three-keys-no-count", "four-keys", "twelve-keys"],
    )
    def test_names_the_first_three_delayed_instruments_then_a_count(self, source, text):
        assert delay_note(source) == text

    @pytest.mark.parametrize(
        "source, text",
        [
            ({"delayed": True}, "delayed ~15 min"),  # an older snapshot without delayed_keys
            ({"delayed": False}, "live"),
            ({}, "live"),
            (None, "live"),
        ],
        ids=["old-snapshot-delayed", "old-snapshot-live", "empty-source", "no-source"],
    )
    def test_falls_back_to_the_header_flag_when_delayed_keys_is_missing(self, source, text):
        assert delay_note(source) == text

    def test_never_says_live_while_the_flag_says_delayed_even_without_keys(self):
        assert "live" not in delay_note({"delayed": True})

    def test_a_key_with_an_ampersand_is_not_pre_escaped(self):
        assert delay_note({"delayed": True, "delayed_keys": ["M&M", "L&T"]}) == (
            "delayed ~15 min: M&M, L&T"
        )

    def test_the_badge_escapes_the_note_exactly_once(self):
        html = run_app_js(
            ["esc", "bd", "delayNote"],
            'console.log(JSON.stringify(bd(`desk dhan+yahoo ${delayNote(P)}`, "warn")))',
            {"delayed": True, "delayed_keys": ["M&M"]},
        )
        assert isinstance(html, str)
        assert "M&amp;M" in html and "&amp;amp;" not in html
        assert html.startswith('<span class="badge warn">') and "delayed ~15 min: M&amp;M" in html
