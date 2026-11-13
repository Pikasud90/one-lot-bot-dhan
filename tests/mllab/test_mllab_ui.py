"""US-069 on the page: the Machine Learning Lab tab, its table and its failure line.

    mllabCell(cell)   one forecast cell
    mllabTable(view)  the whole table from a GET /api/mllab payload
    loadMllab()       one fetch; the table on success, the error in the card on failure

The helpers are lifted out of app.js and run in node, as the paper panel's tests do.
Skipped without node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
APP_JS = UI / "app.js"
ONE_LINERS = ["fmt", "esc", "pct", "tsf", "MLLAB_NAMES", "mllab", "mllabDetail"]
FUNCTIONS = ["mllabCell", "mllabTable", "loadMllab", "mllabTick"]
MODELS = ["arima", "sarima", "garch", "ewma", "empirical"]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def lifted() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    parts = []
    for name in ONE_LINERS:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
        assert found, f"`const {name} = ...;` not found as a single line in app.js"
        parts.append(found.group(0))
    for name in FUNCTIONS:
        found = re.search(rf"^(?:async )?function {name}\(.*?^\}}$", source, re.M | re.S)
        assert found, f"`function {name}(...)` not found in app.js"
        parts.append(found.group(0))
    return "\n".join(parts)


def run(body: str, payload: Any = None) -> Any:
    script = (
        "const els = { 'mllab-tf': { value: '1d' }, 'mllab-table': { innerHTML: '' },"
        " 'mllab-updated': { textContent: '' } };\n"
        "const $ = (id) => els[id];\n"
        "let shown = true; const tabShown = () => shown;\n"
        "globalThis.document = { visibilityState: 'visible' };\n"
        f"const PAYLOAD = {json.dumps(payload)};\n"
        + lifted()
        + f"\n(async () => {{ const out = await (async () => {{ {body} }})();"
        " console.log(JSON.stringify(out)); })();"
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def cell(p_up: float = 0.62, note: str | None = None) -> dict[str, Any]:
    if note:
        return {"note": note, "backtest": None}
    return {
        "mean": 100.0,
        "sd": 1.0,
        "p_up": p_up,
        "lo68": 99.0,
        "hi68": 101.0,
        "lo95": 98.0,
        "hi95": 102.0,
        "n": 500,
        "note": None,
        "detail": "phi=0.1",
        "backtest": {"steps": 100, "in68": 0.7, "in95": 0.94, "direction": 0.52},
    }


def payload() -> dict[str, Any]:
    rows = []
    for i in range(23):
        kind = "INDEX" if i < 5 else "STOCK"
        rows.append(
            {
                "key": f"K{i}",
                "label": f"Inst {i}",
                "kind": kind,
                "last_close": 100.0,
                "last_bar_ts": "2026-11-12T15:15:00+05:30",
                "next_bar_ts": "2026-11-13T09:15:00+05:30",
                "cells": {k: cell() for k in MODELS},
            }
        )
    rows[0]["label"] = "<img src=x onerror=alert(1)>"
    rows[1]["cells"]["garch"] = cell(note='too few bars <b>"60"</b>')
    return {
        "timeframe": "15m",
        "generated_at": "2026-11-13T11:01:00+05:30",
        "models": [{"key": k, "description": f"about {k} <i>"} for k in MODELS],
        "rows": rows,
    }


def test_the_tab_link_and_section_exist():
    html = (UI / "index.html").read_text(encoding="utf-8")
    links = re.findall(r'<a href="#(\w+)" data-tab="\w+">', html)
    assert links[links.index("news") + 1 : links.index("news") + 3] == ["mllab", "settings"]
    assert '<a href="#mllab" data-tab="mllab">Machine Learning Lab</a>' in html
    assert '<section data-tab="mllab" class="tab">' in html
    assert (
        "Statistical forecasts of the next candle's close. They inform; they raise no alert"
        " and no trade." in html
    )
    for tf in ("15m", "30m", "60m", "4h", "1d"):
        assert f"<option>{tf}</option>" in html.split('id="mllab-tf"')[1].split("</select>")[0]


def test_the_table_is_23_rows_by_5_models():
    html = run("return mllabTable(PAYLOAD);", payload())
    body = html.split("<tbody>")[1]
    assert body.count("<tr>") == 23
    assert html.split("<tbody>")[0].count("</th>") == 6  # the instrument + five models
    assert body.count('class="ml-cell') == 23 * 5
    assert "62%" in body and "99.00 – 101.00" in body and "95%: 98.00 – 102.00" in body
    assert "±1.00%" in body
    assert "last 100: 68% band held 70%, direction right 52%" in body
    assert "next candle 2026-11-13 09:15" in body
    assert 'class="ml-cell up"' in body and "--ml-a:0.17" in body


def test_values_from_data_are_escaped():
    html = run("return mllabTable(PAYLOAD);", payload())
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "too few bars &lt;b&gt;&quot;60&quot;&lt;/b&gt;" in html
    assert 'title="about arima &lt;i&gt;"' in html


def test_a_down_cell_is_red_and_a_note_is_muted():
    down = run("return mllabCell(PAYLOAD);", cell(0.3))
    assert 'class="ml-cell down"' in down and '<b class="neg">30%</b>' in down
    noted = run("return mllabCell(PAYLOAD);", cell(note="no closed bars yet"))
    assert '<span class="muted">no closed bars yet</span>' in noted
    no_dir = cell()
    no_dir["backtest"]["direction"] = None
    assert "direction right n/a" in run("return mllabCell(PAYLOAD);", no_dir)


def test_an_error_payload_shows_the_error_in_the_card():
    got = run(
        "globalThis.fetch = async (url) => ({ ok: false, status: 503, url,"
        " json: async () => ({ detail: 'Live Desk <not> running' }) });"
        "await loadMllab(); return [els['mllab-table'].innerHTML, els['mllab-updated'].textContent];"
    )
    assert (
        got[0] == '<div class="warnbar">Forecasts unavailable: Live Desk &lt;not&gt; running</div>'
    )
    assert got[1] == "update failed"


def test_a_good_payload_fills_the_table_for_the_chosen_timeframe():
    got = run(
        "let asked; globalThis.fetch = async (url) => { asked = url; return { ok: true,"
        " status: 200, json: async () => PAYLOAD }; };"
        "await loadMllab(); return [asked, els['mllab-table'].innerHTML.split('<tr>').length - 2,"
        " els['mllab-updated'].textContent];",
        payload(),
    )
    assert got[0] == "/api/mllab?timeframe=1d"
    assert got[1] == 23
    assert got[2] == "updated 2026-11-13 11:01 · 15m"


def test_a_network_failure_is_shown_too():
    got = run(
        "globalThis.fetch = async () => { throw new Error('Failed to fetch'); };"
        "await loadMllab(); return els['mllab-table'].innerHTML;"
    )
    assert "Forecasts unavailable: Failed to fetch" in got


def test_a_thin_history_shows_its_bar_count_and_the_row_note():
    p = payload()
    for r in p["rows"]:
        r["bars"] = 1000
    p["rows"][3]["bars"] = 62
    p["rows"][3]["note"] = "stored bars unreadable (OSError): live bars only"
    html = run("return mllabTable(PAYLOAD);", p)
    assert html.count(" bars</small>") == 1 and '<small class="warn">62 bars</small>' in html
    assert "stored bars unreadable (OSError): live bars only" in html


def test_a_422_detail_list_reads_as_text():
    detail = [
        {"loc": ["query", "timeframe"], "msg": "Field required", "type": "missing"},
        {"loc": ["query", "x"], "msg": "bad <value>", "type": "value_error"},
    ]
    got = run(
        "globalThis.fetch = async () => ({ ok: false, status: 422,"
        f" json: async () => ({{ detail: {json.dumps(detail)} }}) }});"
        "await loadMllab(); return els['mllab-table'].innerHTML;"
    )
    assert "[object Object]" not in got
    assert got == (
        '<div class="warnbar">Forecasts unavailable: Field required; bad &lt;value&gt;</div>'
    )
    assert run("return [mllabDetail({a: 1}), mllabDetail(''), mllabDetail('x')];") == [
        '{"a":1}',
        "",
        "x",
    ]


def test_the_timer_skips_a_tick_while_a_request_is_pending():
    got = run(
        "const calls = []; let answer;"
        "globalThis.fetch = (url) => { calls.push(url);"
        " return new Promise((res) => { answer = res; }); };"
        "const ticks = [mllabTick(), mllabTick(), mllabTick()];"
        "answer({ ok: true, status: 200, json: async () => PAYLOAD });"
        "await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0));"
        "ticks.push(mllabTick()); shown = false; const hidden = mllabTick();"
        "return [ticks, calls.length, hidden];",
        payload(),
    )
    assert got == [[True, False, False, True], 2, False]
