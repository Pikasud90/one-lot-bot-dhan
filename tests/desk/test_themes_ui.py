"""US-070: five selectable themes, switched from the header and remembered across reloads.

The theme lines of app.js run in node over a two-element DOM stub and a localStorage stub.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
sys.path.insert(0, str(UI.parents[2] / "tests" / "acceptance"))
import us030_helpers as H  # noqa: E402

THEMES = ["day", "night", "laxmi", "ramayana", "batman"]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_theme(stored: str | None, choose: str | None = None) -> dict:
    app = (UI / "app.js").read_text(encoding="utf-8")
    store = re.search(r"^const store = .*?\n.*?;$", app, re.M).group(0)
    block = re.search(r"^const THEMES = .*?^applyTheme\(store\.get\(\"theme\", \"day\"\)\);$", app, re.M | re.S).group(0)
    prog = f"""
const mem = {{}}; global.localStorage = {{ getItem: k => mem[k] ?? null, setItem: (k, v) => {{ mem[k] = v; }} }};
const stored = {json.dumps(stored)}; if (stored !== null) mem["onelot.theme"] = JSON.stringify(stored);
const root = {{ dataset: {{}} }}; const sel = {{ value: "" }}; global.document = {{ documentElement: root }};
const $ = (id) => id === "theme-toggle" ? sel : null; const C = () => null;
{store}
{block}
const choose = {json.dumps(choose)}; if (choose) sel.onchange({{ target: {{ value: choose }} }});
console.log(JSON.stringify({{ theme: root.dataset.theme, value: sel.value, stored: mem["onelot.theme"] }}));
"""
    done = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_selector_lists_five_themes_with_day_first():
    html = (UI / "index.html").read_text(encoding="utf-8")
    sel = re.search(r'<select[^>]*id="theme-toggle"[^>]*>(.*?)</select>', html, re.S)
    assert sel and 'aria-label="Theme"' in sel.group(0)
    assert re.findall(r'<option value="([^"]+)"', sel.group(1)) == THEMES


@needs_node
@pytest.mark.parametrize("theme", THEMES)
def test_choosing_a_theme_sets_the_root_and_stores_it(theme):
    got = run_theme(None, theme)
    assert got == {"theme": theme, "value": theme, "stored": json.dumps(theme)}


@needs_node
def test_a_stored_choice_is_applied_on_load_and_night_stays_night():
    assert run_theme("night")["theme"] == "night"
    assert run_theme("batman")["theme"] == "batman"
    assert run_theme(None)["theme"] == "day"
    assert run_theme("nonsense")["theme"] == "day"


@pytest.mark.parametrize("theme", ["laxmi", "ramayana", "batman"])
def test_every_new_theme_block_defines_every_colour_variable_of_day(theme):
    rules = dict(H.css_rules((UI / "styles.css").read_text(encoding="utf-8")))
    names = lambda body: set(re.findall(r"(--[\w-]+)\s*:", body))  # noqa: E731
    day = names(rules[":root"]) - {"--display", "--ui", "--mono", "--cut"}  # fonts and the cut are shared
    block = rules[f':root[data-theme="{theme}"]']
    assert day <= names(block), day - names(block)
    assert "color-scheme" in block
