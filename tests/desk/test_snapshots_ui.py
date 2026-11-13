"""US-071: the Market snapshots card, with the app.js helpers run in node. Skipped without node."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
HELPERS = ["fmt", "cls", "esc", "pct", "snapWhen", "snapTable", "snapList"]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

ROW = {"label": "<b>SBIN</b>", "key": "SBIN", "price": 812.5, "change_pct": 1.2, "change_since_prev_pct": -0.5,
       "bar_ts": "2026-11-13T07:15:00+05:30"}  # fmt: skip
SNAP = {"id": 7, "taken_at": "2026-11-13T01:45:32+00:00", "count": 1, "rows": [ROW]}


def js(expression: str, payload: Any = None) -> Any:
    source = (UI / "app.js").read_text(encoding="utf-8")
    lines = []
    for name in HELPERS:
        found = re.search(rf"^const {name} = .*?;$", source, re.M | re.S)
        assert found, name
        lines.append(found.group(0))
    lines += [
        "const P = JSON.parse(process.argv[1]);",
        f"console.log(JSON.stringify({expression}));",
    ]
    done = subprocess.run(["node", "-e", "\n".join(lines), json.dumps(payload)],
                          capture_output=True, text=True, timeout=30, check=False)  # fmt: skip
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_button_and_the_card_sit_under_the_today_strip():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert (
        "Take market snapshot" in html and 'id="snap-take"' in html and "Market snapshots" in html
    )
    assert (
        html.index('id="today-card"') < html.index('id="snap-card"') < html.index("1. My portfolio")
    )


@needs_node
def test_the_list_renders_newest_first_with_ist_time_and_a_show_control():
    older = {**SNAP, "id": 6, "taken_at": "2026-11-12T10:00:00+00:00"}
    out = js("snapList(P, [])", [SNAP, older])
    assert (
        "13 Nov 2026, 07:15:32" in out
        and "1 prices" in out
        and out.index("13 Nov") < out.index("12 Nov")
    )
    assert out.count("Show") == 2 and "<table" not in out


@needs_node
def test_an_open_snapshot_shows_the_table_with_pos_neg_and_escapes_values():
    out = js("snapList(P, [7])", [SNAP])
    assert "Hide" in out and "Change since the previous snapshot %" in out and "Price time" in out
    assert "&lt;b&gt;SBIN" in out and "<b>SBIN" not in out
    assert 'class="n pos"' in out and 'class="n neg"' in out
    assert "812.50" in out and "-0.50%" in out


@needs_node
def test_nothing_saved_and_a_null_change_render_plainly():
    assert "No snapshots yet." in js("snapList(P, [])", [])
    out = js(
        "snapTable(P)",
        {"rows": [{**ROW, "price": None, "change_since_prev_pct": None, "bar_ts": None}]},
    )
    assert "–" in out


def test_the_error_text_is_shown_in_the_card():
    src = (UI / "app.js").read_text(encoding="utf-8")
    assert "msg.textContent = String(e.message || e)" in src and '$("snap-msg")' in src
