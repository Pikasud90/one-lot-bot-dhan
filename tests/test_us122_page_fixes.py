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


def test_a2_no_first_only_underscore_replace_remains() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert not re.findall(r'\.replace\(\s*"_"', source)


@needs_node
def test_a2_spaced_replaces_every_underscore() -> None:
    assert js(["spaced"], 'spaced("GAP_THROUGH_STOP")') == "GAP THROUGH STOP"
    assert js(["spaced"], "spaced(null)") == ""


INDEX = ROOT / "src" / "onelot" / "ui" / "index.html"
CHEATS = ROOT / "src" / "onelot" / "ui" / "cheats.js"


def test_a3_explorer_says_what_it_shows() -> None:
    page = INDEX.read_text(encoding="utf-8")
    assert "what the backtests actually see" not in page
    assert "the stored bars as they are, before the Lab's cleaning" in page


def test_a4_lab_help_says_what_real_option_prices_are_used_for() -> None:
    page = INDEX.read_text(encoding="utf-8")
    assert "until the Dhan option-chain history is wired" not in page
    assert "Simulator uses them" in page
    assert "the Lab's own runs still price every option with a Black-76 model" in page


def test_a5_settings_marks_mode_and_risk_as_old_engine() -> None:
    page = INDEX.read_text(encoding="utf-8")
    assert "belong to the old engine and do not limit the desk's alerts or its paper trades" in page


def test_a9_cheat_sheet_matches_the_page() -> None:
    cheats = CHEATS.read_text(encoding="utf-8")
    assert "shown in UTC" not in cheats
    assert "Read times on this tab as UTC" not in cheats
    assert "Add 5 hours 30 minutes to the UTC times" not in cheats
    assert "Its title says so, but it does not apply" not in cheats
    assert "The page does not say so" not in cheats
    assert "Option prices are not stored here for most history" not in cheats


@needs_node
def test_a6_build_changed_compares_the_two_builds() -> None:
    out = js(
        ["buildChanged"],
        '[buildChanged("a1", "a1"), buildChanged("a1", "b2"), buildChanged("", "b2"),'
        ' buildChanged("a1", null), buildChanged(undefined, undefined)]',
    )
    assert out == [False, True, False, False, False]


@needs_node
def test_a7_row_says_real_price_or_model_price_beside_the_instrument() -> None:
    from tests.desk.test_paper_panel_ui import js as paper_js
    from tests.desk.test_paper_panel_ui import trade

    real = paper_js("paperRow(P)", trade(price_source="real"))
    model = paper_js("paperRow(P)", trade(price_source="model"))
    plain = paper_js("paperRow(P)", trade())
    assert '<span class="dim">· entry at a real price</span>' in real
    assert '<span class="dim">· entry at a model price</span>' in model
    assert "real price" not in plain and "model price" not in plain
    assert "price_source" not in plain
    # a row without the key renders as before: the others only add the one span
    assert real.replace(' <span class="dim">· entry at a real price</span>', "") == plain
