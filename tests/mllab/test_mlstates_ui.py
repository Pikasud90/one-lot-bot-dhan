"""US-077 on the page: the Market states card. Helpers are lifted out of mlstates.js and run
in node (skipped without node); the CSS holds no colour literal."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
JS = UI / "mlstates.js"
CSS = UI / "mlstates.css"
ONE_LINERS = ["esc", "DIRS", "ARROW", "pc", "upper"]
FUNCTIONS = [
    "msContext", "msHeadline", "msSizeLine", "msBars", "msChips", "msRow", "msPattern",
    "msRuns", "msEvidence", "msLegend", "msThin", "msCard",
]  # fmt: skip

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def lifted() -> str:
    source = JS.read_text(encoding="utf-8")
    parts = ["const O = {};"]
    for name in ONE_LINERS:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
        assert found, f"`const {name} = ...;` not found as a single line"
        parts.append(found.group(0))
    for name in FUNCTIONS:
        found = re.search(rf"^function {name}\(.*?^\}}$", source, re.M | re.S)
        assert found, f"`function {name}(...)` not found"
        parts.append(found.group(0))
    return "\n".join(parts)


def run(body: str) -> Any:
    script = lifted() + f"\nconsole.log(JSON.stringify((() => {{ {body} }})()));"
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def nxt(lean="bull", conviction="weak", score=31, p=0.38) -> str:
    probs = {"bull": 0.2, "flat": 0.3, "bear": 0.2}
    probs[lean] = p
    return json.dumps(
        {"context": ["bull", "bull"], "order": 2, "n": 212, "probs": probs, "lean": lean,
         "edge_pts": 5, "z": 1.2, "conviction": conviction, "score": score}
    )  # fmt: skip


BASE = '{"bull": 0.33, "flat": 0.4, "bear": 0.27, "active": 0.48}'


@needs_node
class TestHeadline:
    def test_a_weak_lean(self):
        assert run(f"return msHeadline({nxt()}, {BASE});") == (
            "Next session: leans BULL 38% (usual 33%) · conviction weak (score 31)"
        )

    @pytest.mark.parametrize("conv", ["moderate", "strong"])
    def test_stronger_lean(self, conv):
        text = run(f"return msHeadline({nxt('bear', conv, 80, 0.45)}, {BASE});")
        assert f"leans BEAR 45% (usual 27%) · conviction {conv} (score 80)" in text

    def test_no_lean_when_conviction_is_none(self):
        text = run(f"return msHeadline({nxt('bull', 'none', 12)}, {BASE});")
        assert text == "Next session: no lean: tomorrow looks like any other session (score 12)"
        assert "leans" not in text and "BULL" not in text

    def test_size_line(self):
        sz = {"context": ["active"], "n": 900, "p_active": 0.61, "base_active": 0.48,
              "lean": "active", "edge_pts": 13, "z": 3.4, "conviction": "strong", "score": 85}  # fmt: skip
        assert run(f"return msSizeLine({json.dumps(sz)});") == (
            "Next session's range: leans ACTIVE 61% (usual 48%) · conviction strong"
        )
        sz.update(lean="calm", p_active=0.40)
        assert "leans CALM 60% (usual 52%)" in run(f"return msSizeLine({json.dumps(sz)});")
        sz.update(conviction="none")
        assert run(f"return msSizeLine({json.dumps(sz)});").startswith(
            "Next session's range: no lean"
        )

    def test_context_names_the_sessions_and_the_count(self):
        assert run(f"return msContext({nxt()});") == (
            "from the last two sessions: bull, bull (n = 212)"
        )
        one = json.dumps({"context": ["flat"], "n": 70})
        assert run(f"return msContext({one});") == "from the last session: flat (n = 70)"


@needs_node
class TestPieces:
    def test_bars_have_a_tick_at_the_base_rate(self):
        html = run(f"return msBars({nxt()}, {BASE});")
        assert html.count("ms-tick") == 3 and "left:33.0%" in html and "width:38.0%" in html
        assert 'class="ms-bar bull lean"' in html and 'class="ms-bar bear"' in html

    def test_no_lean_bar_is_not_marked(self):
        assert " lean" not in run(f"return msBars({nxt('bull', 'none')}, {BASE});")

    def test_chips_last_30_newest_right_with_title(self):
        sessions = [
            {"date": f"2026-10-{i % 28 + 1:02d}", "r_pct": 0.5 if i % 2 else -1.25,
             "z": 1, "direction": "bull" if i % 2 else "bear",
             "size": "active" if i % 3 == 0 else "calm"}
            for i in range(60)
        ]  # fmt: skip
        html = run(f"return msChips({json.dumps(sessions)});")
        assert html.count("ms-chip ") == 30
        assert "▲" in html and "▼" in html
        assert "+0.50%" in html and "−1.25%" in html  # a real minus sign
        assert html.rfind("2026-10-") > html.find("2026-10-")
        assert "ms-chip bull active" in html and "ms-chip bear calm" in html

    def test_chips_when_empty(self):
        assert "No stated sessions" in run("return msChips([]);")

    def test_escaping(self):
        evil = "<img src=x onerror=alert(1)>"
        sessions = [{"date": evil, "r_pct": 1, "z": 1, "direction": evil, "size": evil}]
        out = run(f"return msChips({json.dumps(sessions)});")
        assert "<img" not in out and "&lt;img" in out
        card = run(
            f"return msCard({{note: {json.dumps(evil)}}}, "
            f"[{{key: 'K', label: {json.dumps(evil)}}}], 'K', true);"
        )
        assert "<img" not in card and "&lt;img" in card

    def test_evidence_wording(self):
        wf = {"n": 900, "hit": 0.41, "hit_naive": 0.43, "size_hit": 0.58, "size_hit_naive": 0.5}
        text = run(f"return msEvidence({json.dumps(wf)});")
        assert text.startswith(
            "Checked on 900 past sessions: it named the next direction right 41% of the time; "
            "always naming the most common one scores 43%."
        )
        assert "58% against 50%" in text
        assert "Not enough" in run("return msEvidence({n: 0});")

    def test_card_has_selector_legend_open_flag_and_no_claim_without_data(self):
        card = run("return msCard(null, [{key: 'A', label: 'Alpha'}], 'A', true);")
        assert '<select id="ms-select"' in card and "<details" in card and " open" in card
        assert '<details class="ms-legend">' in run("return msCard(null, [], null, false);")
        legend = run("return msLegend();")
        assert "not an alert and not a trade" in legend and "Move size" in legend
        assert "opening candle" in legend and "no more often than a coin" in legend

    def test_tables(self):
        d = {
            "last": ["bull", "bull"],
            "base": {"bull": 0.33, "flat": 0.4, "bear": 0.27},
            "first_order": [{"context": [k], "n": 100, "probs": {"bull": 0.3, "flat": 0.4, "bear": 0.3}}
                            for k in ("bull", "flat", "bear")],
            "second_order": [{"context": [a, b], "n": 59,
                              "probs": {"bull": 0.2, "flat": 0.5, "bear": 0.3}}
                             for a in ("bull", "flat", "bear") for b in ("bull", "flat", "bear")],
        }  # fmt: skip
        html = run(f"return msPattern({json.dumps(d)});")
        assert "last session: bull" in html and "last two: bull, bull" in html
        assert ">59<" in html and ">100<" in html
        runs = [{"direction": "bull", "length": 3, "continued": 0.3, "n": 40},
                {"direction": "bear", "length": 1, "continued": None, "n": 0}]  # fmt: skip
        out = run(f"return msRuns({json.dumps(runs)}, {BASE});")
        assert "3 or more" in out and "30%" in out and "–" in out


def test_the_css_has_no_colour_literal():
    css = CSS.read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "hex colour in mlstates.css"
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", css)
    assert not re.search(
        r"(?:color|background|border[\w-]*)\s*:\s*(?:white|black|red|green|blue)\b", css
    )
    assert "var(--ok)" in css and "var(--bad)" in css


def test_the_js_has_no_colour_literal_and_the_root_exists():
    js = JS.read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{6}\b|\brgba?\(", js)
    assert 'id="mlstates-root"' in (UI / "index.html").read_text(encoding="utf-8")


@needs_node
class TestSizeChipsAndThinCard:
    def test_an_active_session_chip_is_filled_and_says_wide(self):
        s = [
            {"date": "2026-11-12", "r_pct": 1, "z": 1, "direction": "bull", "size": "active"},
            {"date": "2026-11-13", "r_pct": 1, "z": 1, "direction": "bull", "size": "calm"},
        ]
        html = run(f"return msChips({json.dumps(s)});")
        assert 'class="ms-chip bull active" title="2026-11-12: +1.00%, bull, wide range"' in html
        assert 'class="ms-chip bull calm" title="2026-11-13: +1.00%, bull, narrow range"' in html

    def test_the_thin_card_shows_only_the_line_the_strip_and_the_legend(self):
        d = {
            "thin": True, "n_states": 3, "next": {"context": ["bull"], "n": 1,
            "probs": {"bull": 0.7, "flat": 0.3, "bear": 0}, "lean": "bull",
            "conviction": "none", "score": 2},
            "base": {"bull": 0.67, "flat": 0.33, "bear": 0, "active": 0.5},
            "runs": [{"direction": "bull", "length": 1, "continued": 1, "n": 1}],
            "sessions": [{"date": "2026-11-13", "r_pct": 1, "z": 1, "direction": "bull",
                          "size": "active"}],
            "first_order": [], "second_order": [], "last": ["bull"],
            "walk_forward": {"n": 0},
        }  # fmt: skip
        html = run(f"return msCard({json.dumps(d)}, [{{key: 'K', label: 'K'}}], 'K', true);")
        assert (
            "Only 3 stated sessions for this instrument: too few to read anything from. "
            "The card fills in once there are 250." in html
        )
        assert "ms-chip" in html and "How to read this page" in html
        card = re.sub(r"title=\"[^\"]*\"", "", html.split("<details")[0])
        for bad in ("ms-bar", "ms-tick", "%", "conviction", "score", "usual", "ms-table", "Runs"):
            assert bad not in card, bad
