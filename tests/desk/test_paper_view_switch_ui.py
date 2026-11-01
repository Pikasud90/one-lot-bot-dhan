"""US-032 UI: the 'Today | Last session | All' control of the Paper panel (developer tests).

    PAPER_VIEWS, paperViewOf(v)      the three views; anything else is "today"
    paperViewKey(view, key)          arrow keys / Home / End move along the radio group
    showPaperView(view)              select it: aria-checked + roving tabindex on the buttons,
                                     remembered in localStorage (through `store`, which never
                                     throws), and the last payload re-rendered at once
    paperViewClick / paperViewKeydown  the document's click and keydown handlers

The buttons are static markup in index.html (a role="radiogroup"); app.js only toggles them.
No browser: the definitions are lifted out of app.js and run in node over a fake DOM and a
fake localStorage that can be made to throw (a private window). Skipped without node.
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
APP_JS = (UI / "app.js").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")
STYLES = (UI / "styles.css").read_text(encoding="utf-8")

HARNESS = r"""
let stored = {}, throwing = false, focused = null, prevented = 0;
const localStorage = {
  getItem: (k) => { if (throwing) throw new Error("denied"); return k in stored ? stored[k] : null; },
  setItem: (k, v) => { if (throwing) throw new Error("denied"); stored[k] = String(v); },
};
const buttons = ["today", "last", "all"].map((v) => ({
  dataset: { paperView: v }, attrs: {}, tabIndex: 0,
  setAttribute(k, val) { this.attrs[k] = String(val); },
  focus() { focused = v; },
  closest(sel) { return sel === "[data-paper-view]" ? this : null; },
}));
const elsewhere = { closest: () => null };
const els = { "desk-paper": { innerHTML: "OLD" }, "desk-paper-meta": { textContent: "OLD", className: "muted" } };
const $ = (id) => els[id];
const document = {
  querySelectorAll: (sel) => (sel === "[data-paper-view]" ? buttons : []),
  querySelector: (sel) => { const m = /data-paper-view="(\w+)"/.exec(sel); return m ? buttons.find((b) => b.dataset.paperView === m[1]) || null : null; },
};
const paperPanel = (p, v) => `PANEL ${v}`;
const paperMeta = (p, v) => `META ${v}`;
const btn = (v) => buttons.find((b) => b.dataset.paperView === v);
const key = (target, k) => ({ target, key: k, preventDefault() { prevented++; } });
const state = () => ({
  view: paperFeed.view, stored, focused, prevented,
  checked: buttons.filter((b) => b.attrs["aria-checked"] === "true").map((b) => b.dataset.paperView),
  tabbable: buttons.filter((b) => b.tabIndex === 0).map((b) => b.dataset.paperView),
  panel: els["desk-paper"].innerHTML, meta: els["desk-paper-meta"].textContent,
});
"""

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def definition(name: str) -> str:
    """A top-level definition: `const name = ...;` on one line (`store` spans two) or a
    `function name(...) {...}` block."""
    if name == "store":
        found = re.search(r"^const store = \{.*?\} \};$", APP_JS, re.M | re.S)
    else:
        found = re.search(rf"^const {name} = .*;$", APP_JS, re.M) or re.search(
            rf"^function {name}\(.*?^\}}$", APP_JS, re.M | re.S
        )
    assert found, f"{name} is not a top-level definition in app.js"
    return found.group(0)


NAMES = [
    "store",
    "PAPER_VIEWS",
    "paperViewOf",
    "paperViewKey",
    "paperFeed",
    "showPaperView",
    "paperViewClick",
    "paperViewKeydown",
]


def run(scenario: str) -> Any:
    script = "\n".join(
        [
            HARNESS,
            *(definition(n) for n in NAMES),
            scenario,
            "console.log(JSON.stringify(state()));",
        ]
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def value(expression: str) -> Any:
    script = "\n".join(
        [HARNESS, *(definition(n) for n in NAMES), f"console.log(JSON.stringify({expression}));"]
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestViews:
    def test_three_views_in_order(self):
        assert value("PAPER_VIEWS") == ["today", "last", "all"]

    @pytest.mark.parametrize(
        ("raw", "view"),
        [("today", "today"), ("last", "last"), ("all", "all"), ("ALL", "today")]
        + [("constructor", "today"), (None, "today"), (3, "today")],
    )
    def test_anything_else_is_today(self, raw, view):
        assert value(f"paperViewOf({json.dumps(raw)})") == view

    @pytest.mark.parametrize(
        ("view", "key", "next_view"),
        [
            ("today", "ArrowRight", "last"),
            ("last", "ArrowDown", "all"),
            ("all", "ArrowRight", "today"),  # wraps, as a radio group does
            ("today", "ArrowLeft", "all"),
            ("last", "ArrowUp", "today"),
            ("all", "Home", "today"),
            ("today", "End", "all"),
            ("today", "Enter", None),  # the button's own click handles Enter and Space
            ("today", " ", None),
            ("today", "a", None),
        ],
    )
    def test_the_keys_of_a_radio_group(self, view, key, next_view):
        assert value(f"paperViewKey({json.dumps(view)}, {json.dumps(key)})") == next_view


class TestShowPaperView:
    def test_selects_one_button_and_makes_only_it_tabbable(self):
        got = run('showPaperView("last");')
        assert got["view"] == "last"
        assert got["checked"] == ["last"] and got["tabbable"] == ["last"]

    def test_remembers_the_choice(self):
        got = run('showPaperView("all");')
        assert json.loads(got["stored"]["onelot.paper.view"]) == "all"

    def test_rerenders_the_last_answer_at_once(self):
        got = run('paperFeed.last = { enabled: true }; showPaperView("last");')
        assert (got["panel"], got["meta"]) == ("PANEL last", "META last")

    def test_before_any_answer_nothing_is_rendered(self):
        got = run('showPaperView("all");')
        assert (got["panel"], got["meta"]) == ("OLD", "OLD")

    def test_a_failure_line_is_left_alone(self):
        got = run(
            'paperFeed.last = { enabled: true }; $("desk-paper-meta").className = "warn";'
            'showPaperView("all");'
        )
        assert got["panel"] == "PANEL all" and got["meta"] == "OLD"

    def test_an_unknown_view_selects_today(self):
        got = run('showPaperView("nonsense");')
        assert got["view"] == "today" and got["checked"] == ["today"]

    def test_a_browser_without_storage_still_switches(self):
        got = run('throwing = true; showPaperView("last");')
        assert got["view"] == "last" and got["checked"] == ["last"] and got["stored"] == {}

    @pytest.mark.parametrize(
        ("setup", "view"),
        [
            ('stored["onelot.paper.view"] = JSON.stringify("all");', "all"),
            ('stored["onelot.paper.view"] = "not json";', "today"),
            ('stored["onelot.paper.view"] = JSON.stringify("x");', "today"),
            ("throwing = true;", "today"),
            ("", "today"),
        ],
        ids=["remembered", "corrupt", "unknown", "storage-throws", "first-visit"],
    )
    def test_the_start_up_view(self, setup, view):
        got = run(f'{setup} showPaperView(store.get("paper.view", "today"));')
        assert got["view"] == view


class TestHandlers:
    def test_a_click_on_a_button_selects_it(self):
        got = run('paperViewClick({ target: btn("all") });')
        assert got["view"] == "all" and got["checked"] == ["all"]

    def test_a_click_elsewhere_changes_nothing(self):
        got = run('showPaperView("last"); paperViewClick({ target: elsewhere });')
        assert got["view"] == "last"

    def test_an_arrow_key_moves_the_selection_and_the_focus(self):
        got = run('paperViewKeydown(key(btn("today"), "ArrowRight"));')
        assert (got["view"], got["focused"], got["prevented"]) == ("last", "last", 1)

    def test_other_keys_are_left_to_the_browser(self):
        got = run(
            'paperViewKeydown(key(btn("today"), "Tab")); paperViewKeydown(key(elsewhere, "ArrowRight"));'
        )
        assert (got["view"], got["prevented"]) == ("today", 0)


class TestMarkup:
    def control(self) -> str:
        live = re.search(r'<section data-tab="live"[^>]*>(.*?)</section>', INDEX, re.S)
        assert live
        card = re.search(r'id="desk-paper-card">(.*?)\n    </div>', live.group(1), re.S)
        assert card, "the Paper card is missing"
        found = re.search(r'<div class="paper-views"[^>]*>.*?</div>', card.group(1), re.S)
        assert found, "the Paper card has no .paper-views control"
        return found.group(0)

    def test_a_labelled_radio_group_of_three_buttons(self):
        html = self.control()
        assert 'role="radiogroup"' in html and 'aria-label="' in html
        buttons = re.findall(r"<button([^>]*)>([^<]*)</button>", html)
        assert [label for _, label in buttons] == ["Today", "Last session", "All"]
        for attrs, _ in buttons:
            assert 'type="button"' in attrs and 'role="radio"' in attrs
        views = [re.search(r'data-paper-view="(\w+)"', a).group(1) for a, _ in buttons]
        assert views == ["today", "last", "all"]

    def test_today_is_selected_and_tabbable_before_any_script_runs(self):
        buttons = re.findall(r"<button([^>]*)>", self.control())
        checked = ['aria-checked="true"' in a for a in buttons]
        tabbable = ['tabindex="0"' in a for a in buttons]
        assert checked == tabbable == [True, False, False]

    def test_the_control_uses_variables_only_and_skews_on_before(self):
        rules = [
            (sel.strip(), body)
            for sel, body in re.findall(
                r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", STYLES, flags=re.S)
            )
            if "paper-views" in sel
        ]
        assert rules, "no .paper-views CSS"
        for sel, body in rules:
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|\bwhite\b|\bblack\b", body), sel
        before = [body for sel, body in rules if sel.endswith("::before")]
        assert any("skew(" in body for body in before), "the shape is not skewed on ::before"
        assert not any("skew(" in body for sel, body in rules if not sel.endswith("::before"))
        sizes = [
            float(px)
            for _, body in rules
            for px in re.findall(r"font(?:-size)?\s*:\s*(?:\d{3}\s+)?([\d.]+)px", body)
        ]
        assert sizes and min(sizes) >= 14, sizes  # large type: the desk's buttons are 14px
