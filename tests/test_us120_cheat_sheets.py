"""US-120: the cheat sheet block of every tab (src/onelot/ui/cheats.js). The tests read the data
between the two marker lines, so they hold after the content is replaced. Node runs the page code."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "src" / "onelot" / "ui"
HTML = (UI / "index.html").read_text("utf-8")
CSS = (UI / "styles.css").read_text("utf-8")
JS_PATH = UI / "cheats.js"
JS = JS_PATH.read_text("utf-8")
KEYS = ["home", "live", "volume", "sim", "lab", "data", "news", "mllab", "settings"]
FIELDS = ["what", "read", "use", "trust", "dont"]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def node(code: str) -> str:
    out = subprocess.run(
        ["node", "-e", code], capture_output=True, text=True, timeout=30, check=False
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def data_block() -> str:
    m = re.search(r"// CHEATS-DATA-BEGIN\n(.*?)\n// CHEATS-DATA-END", JS, re.S)
    assert m, "the CHEATS data markers are missing"
    return m.group(1)


def load_data() -> dict:
    code = data_block() + "\nprocess.stdout.write(JSON.stringify(CHEATS));"
    return json.loads(node(code))


def strings(entry: dict):
    for k in ("what", "use", "dont"):
        yield from entry[k]
    for r in entry["read"]:
        yield r["term"]
        yield r["meaning"]
    for r in entry["trust"]:
        yield r["text"]


def run_page(expr: str, data_js: str | None = None) -> str:
    src = JS if data_js is None else JS.replace(data_block(), data_js)
    code = "const document = undefined;\n" + src + "\nprocess.stdout.write(String(" + expr + "));"
    return node(code)


def test_every_section_has_one_cheat_first():
    for k in KEYS:
        m = re.search(rf'<section data-tab="{k}" class="tab">\s*(.*?)\n', HTML)
        assert m, k
        first = m.group(1)
        assert first.startswith(f'<details class="cheat" data-cheat="{k}">'), k
        assert HTML.count(f'data-cheat="{k}"') == 1
        assert '<div class="cheat-body"></div>' in first and "<summary>" in first
    assert HTML.count('class="cheat"') == 9


def test_script_before_app_js():
    a = HTML.index('src="/static/cheats.js"')
    assert a < HTML.index('src="/static/app.js"')
    assert re.search(r'<script src="/static/cheats.js" defer></script>', HTML)


def test_data_shape():
    data = load_data()
    assert list(data) == KEYS
    for k, e in data.items():
        assert list(e) == FIELDS or set(e) == set(FIELDS), k
        for f in FIELDS:
            assert e[f], (k, f)
        assert len(e["read"]) >= 5 and len(e["trust"]) >= 3, k
        assert all(r["term"] and r["meaning"] for r in e["read"])
        assert all(r["level"] in {"real", "estimate", "gap"} and r["text"] for r in e["trust"])
        assert any(r["level"] == "gap" for r in e["trust"]), k


def test_sentences_short_and_no_dashes():
    for k, e in load_data().items():
        for s in strings(e):
            assert "—" not in s and "–" not in s, (k, s)
            for sentence in re.split(r"(?<=[.!?])\s+", s):
                assert len(sentence) <= 240, (k, sentence)


@needs_node
def test_node_check():
    out = subprocess.run(["node", "--check", str(JS_PATH)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@needs_node
def test_html_has_headings_in_order_and_tags():
    html = run_page('cheatHtml("home")')
    heads = [
        "What this tab is",
        "How to read it",
        "How to use it on a market day",
        "How far to trust it",
        "Do not",
    ]
    pos = [html.index(f"<h4>{h}</h4>") for h in heads]
    assert pos == sorted(pos)
    for tag in ("Real data:", "Estimate:", "Gap:"):
        assert tag in html


@needs_node
def test_escapes_and_missing():
    evil = (
        'const CHEATS = {x:{what:["<script>alert(1)</script>"],read:[{term:"<b>",meaning:"&\\"\'"}],'
        'use:["u"],trust:[{level:"real",text:"<i>"}],dont:["d"]}};'
    )
    html = run_page('cheatHtml("x")', evil)
    assert "<script>" not in html and "<b>" not in html and "<i>" not in html
    assert "&lt;script&gt;" in html and "&amp;&quot;&#39;" in html
    assert run_page('cheatHtml("nope")') == ""
    partial = 'const CHEATS = {x:{what:["w"]}};'
    out = run_page('cheatHtml("x")', partial)
    assert "What this tab is" in out and "How to read it" not in out


@needs_node
def test_mount_fills_body_and_never_throws():
    code = """
const body = {innerHTML: ""};
const el = {getAttribute: () => "home", querySelector: () => body};
const root = {querySelectorAll: () => [el]};
cheatMount(root);
cheatMount({querySelectorAll: () => []});
cheatMount({});
process.stdout.write(body.innerHTML.includes("What this tab is") ? "ok" : "bad");
"""
    assert run_page("0").endswith("0")
    src = "const document = undefined;\n" + JS + "\n" + code
    assert node(src) == "ok"


def test_css_floor_and_no_colour_literals():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    start = css.index(".cheat{")
    block = css[start:]
    assert block.count("{") >= 10
    for px in re.findall(r"font-size:\s*([\d.]+)px", block):
        assert float(px) >= 14, px
    assert not re.search(r"font-size:\s*(?!var)[^;}]*(small|smaller)", block)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", block)
    names = r"\b(white|black|red|green|blue|gray|grey|orange|yellow|purple|pink|cyan|teal)\b"
    decls = " ".join(re.findall(r":([^;{}]*)[;}]", block))
    assert not re.search(names, re.sub(r"var\([^)]*\)", "", decls))
