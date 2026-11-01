"""US-032 AC2: the 'Today | Last session | All' control, its keyboard, and its memory.

AC2: "a segmented control 'Today | Last session | All' ... the owner can switch views ... the
choice is remembered per browser (localStorage wrapped in try/catch) and the page works without
it."

Static facts come from the real index.html (a labelled radiogroup of three radio buttons, one
checked and tabbable, the control legible in both themes). The behaviour runs the REAL page code
of app.js in node over a fake browser: the fake DOM is built from the real markup (buttons are
found by the attribute selectors app.js itself uses, so no attribute name is assumed), clicks and
key presses go through the listeners app.js registers at load, the data comes from the REAL
/api/paper on the owner's Sunday, and localStorage can be pre-filled, corrupt, or throwing (a
private window). Skipped, with a reason, on a machine without node.
"""

from __future__ import annotations

import json
import random
import re

import pytest
import test_us030_b_static_ui as chips  # the CSS variable / contrast readers of US-030
import us001_helpers as base
import us030_helpers as H
import us032_helpers as V

from onelot.config import AppConfig

INDEX = H.INDEX_HTML.read_text(encoding="utf-8")
STYLES = H.STYLES_CSS.read_text(encoding="utf-8")
TEXTS = ["Today", "Last session", "All"]

needs_node = pytest.mark.skipif(
    H.NODE is None, reason="node is not installed; app.js helpers not exercised"
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    base.block_network(monkeypatch)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    H.freeze_clock(monkeypatch, V.SUNDAY)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = base.new_db(tmp_path, "us032-control.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


@pytest.fixture
def payload(app_cfg, db) -> dict:
    """The owner's Sunday as the real endpoint serves it."""
    app = H.make_app(app_cfg, db)
    rows, _ = V.owners_sunday()
    H.insert_trades(app.db.engine, rows)
    return H.served(app)


@pytest.fixture
def served(payload) -> str:
    """The same payload as the fake server's answer."""
    return V.answer_with(payload)


# ══ the markup ════════════════════════════════════════════════════════════════════════
class TestTheControlMarkup:
    def test_three_radio_buttons_read_today_last_session_all(self):
        buttons = V.control_buttons(INDEX)
        assert [" ".join(b["text"].split()) for b in buttons] == TEXTS

    def test_the_group_is_a_labelled_radiogroup_inside_the_paper_card(self):
        live = H.live_section(INDEX)
        group = re.search(r'<[^>]*role="radiogroup"[^>]*>', live)
        assert group, "no role=radiogroup on the Live Desk page"
        assert re.search(r'aria-label(?:ledby)?="[^"]+"', group.group(0)), group.group(0)
        card, table = live.index('id="desk-paper-card"'), live.index('id="desk-paper"')
        assert card < live.index(group.group(0)) < table, "the control belongs above the table"

    def test_they_are_real_buttons_that_never_submit_anything(self):
        live = H.live_section(INDEX)
        radios = re.findall(r"<button[^>]*role=\"radio\"[^>]*>", live)
        assert len(radios) == 3 and all('type="button"' in r for r in radios)

    def test_today_is_the_one_checked_and_tabbable_radio_at_load(self):
        buttons = V.control_buttons(INDEX)
        checked = [b["attrs"].get("aria-checked") for b in buttons]
        tabs = [b["attrs"].get("tabindex") for b in buttons]
        assert checked == ["true", "false", "false"] and tabs == ["0", "-1", "-1"]

    def test_a_focus_ring_is_defined_for_the_keyboard(self):
        ring = [
            body
            for selector, body in H.css_rules(STYLES)
            if ".paper-views" in selector and "focus" in selector
        ]
        assert any(re.search(r"outline\s*:\s*(?!none|0)", b) for b in ring), ring


@pytest.mark.parametrize("night", [False, True], ids=["day", "night"])
class TestTheControlIsLegibleInBothThemes:
    @staticmethod
    def style(checked: bool) -> tuple[str, str]:
        text = background = None
        for selector, body in H.css_rules(STYLES):
            sel = re.sub(r"\s+", " ", selector.strip())
            decls = chips.declarations(body)
            if sel == ".paper-views button" or (
                checked and sel == '.paper-views button[aria-checked="true"]'
            ):
                text = decls.get("color", text)
            if sel == ".paper-views button::before" or (
                checked and sel == '.paper-views button[aria-checked="true"]::before'
            ):
                background = decls.get("background", background)
        assert text and background, (
            f"no colours found for the {'checked' if checked else 'plain'} button"
        )
        return text, background

    @pytest.mark.parametrize("checked", [False, True], ids=["unchecked", "checked"])
    def test_the_label_is_readable_on_its_background(self, night, checked):
        variables = chips.theme_variables(night)
        panel = chips.rgba(variables["--panel"], variables)[:3]
        text, background = self.style(checked)
        bg = chips.over(chips.rgba(background, variables), panel)
        ratio = chips.contrast(chips.over(chips.rgba(text, variables), bg), bg)
        assert ratio >= 4.5, f"{ratio:.2f}:1 ({text} on {background})"


# ══ the behaviour on the real page code ═══════════════════════════════════════════════
def which_view(html: str) -> str:
    """Tell the three views of the owner's Sunday apart: the fallback shows today's two rows and
    the session under a heading (69 rows), 'Last session' the session alone (67), 'All' every
    trade with no session heading (69)."""
    names = V.instruments(html)
    headed = "last session" in V.text_of(html)
    if headed and "HDFCBANK" in names and "TCS" in names and len(names) == 69:
        return "today"
    if headed and "HDFCBANK" not in names and len(names) == 67:
        return "last"
    if not headed and len(names) == 69:
        return "all"
    return f"unknown({len(names)}, headed={headed})"


def load(served: str, *scenario: str, **kw) -> dict:
    return V.run_views("snapshot(); await advance(0);" + "".join(scenario), answer=served, **kw)


@needs_node
class TestSwitching:
    def test_the_page_opens_on_today_with_the_fallback(self, served):
        got = load(served)
        assert got["controls"]["checked"] == ["Today"] and got["controls"]["tabbable"] == ["Today"]
        assert which_view(V.paper_html(got)) == "today"

    @pytest.mark.parametrize(
        "text, view", [("Today", "today"), ("Last session", "last"), ("All", "all")]
    )
    def test_clicking_a_button_shows_that_view_at_once_without_a_new_request(
        self, served, text, view
    ):
        got = load(served, f"click({json.dumps(text)}); await advance(0);")
        assert which_view(V.paper_html(got)) == view
        assert got["controls"]["checked"] == [text] and got["controls"]["tabbable"] == [text]
        assert len(got["fetches"]) == 1, "switching a view must not hit the server again"

    def test_the_day_line_of_last_session_is_the_sessions_totals(self, served):
        line = H.paper_element(load(served, 'click("Last session");'), "meta")["textContent"]
        expected = V.totals_of(V.friday_grosses())
        assert "last session" in line.lower() and f"{expected['closed']} closed" in line
        assert expected["gross_rupees"] in H.money_text(line)

    def test_the_day_line_of_all_does_not_present_one_days_totals(self, served):
        line = H.paper_element(load(served, 'click("All");'), "meta")["textContent"]
        assert line and V.totals_of(V.friday_grosses())["gross_rupees"] not in H.money_text(line)

    def test_a_click_outside_the_control_changes_nothing(self, served):
        got = load(served, "click(null);")
        assert got["controls"]["checked"] == ["Today"]

    def test_a_new_snapshot_refreshes_the_table_in_the_chosen_view(self, served):
        got = load(served, 'click("All"); snapshot(); await advance(6000);')
        assert len(got["fetches"]) == 2
        assert which_view(V.paper_html(got)) == "all"

    def test_a_choice_made_before_the_first_answer_applies_to_the_first_render(self, served):
        got = V.run_views('click("Last session"); snapshot(); await advance(0);', answer=served)
        assert which_view(V.paper_html(got)) == "last"
        assert got["controls"]["checked"] == ["Last session"]

    def test_switching_never_hides_a_failure_warning_on_the_day_line(self, payload):
        body = payload
        serve = (
            f"__answer = () => __fail ? {{ throw: 'down' }} : "
            f"{{ status: 200, body: {json.dumps(body)} }};"
        )
        got = V.run_views(
            serve
            + "snapshot(); await advance(0); __fail = true; snapshot(); await advance(6000);"
            + 'click("All"); await advance(0);'
        )
        meta = H.paper_element(got, "meta")
        assert "warn" in meta["className"] and "unavailable" in meta["textContent"]
        assert which_view(V.paper_html(got)) == "all", "the table must still switch"


@needs_node
class TestKeyboard:
    @pytest.mark.parametrize(
        "start, key, end",
        [
            ("Today", "ArrowRight", "Last session"),
            ("Last session", "ArrowRight", "All"),
            ("All", "ArrowRight", "Today"),
            ("Today", "ArrowLeft", "All"),
            ("All", "ArrowLeft", "Last session"),
            ("Last session", "ArrowLeft", "Today"),
            ("Today", "ArrowDown", "Last session"),
            ("All", "ArrowUp", "Last session"),
            ("Last session", "Home", "Today"),
            ("Today", "End", "All"),
        ],
    )
    def test_arrows_wrap_and_home_end_jump_along_the_radio_group(self, served, start, key, end):
        got = load(
            served, f"click({json.dumps(start)}); press({json.dumps(key)}, {json.dumps(start)});"
        )
        controls = got["controls"]
        assert controls["checked"] == [end] and controls["tabbable"] == [end]
        assert controls["focused"] == end, "focus must follow the selection"
        assert controls["prevented"] == 1, "a handled key must not also scroll the page"
        assert (
            which_view(V.paper_html(got))
            == {"Today": "today", "Last session": "last", "All": "all"}[end]
        )

    @pytest.mark.parametrize("key", ["Tab", "Enter", " ", "a", "Escape", "PageDown", "Shift"])
    def test_other_keys_are_left_to_the_browser(self, served, key):
        got = load(served, f"press({json.dumps(key)}, 'Today');")
        assert got["controls"]["checked"] == ["Today"] and got["controls"]["prevented"] == 0

    @pytest.mark.parametrize("modifier", ["ctrlKey", "altKey", "metaKey", "shiftKey"])
    @pytest.mark.parametrize(
        "key", ["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp", "Home", "End"]
    )
    def test_a_key_pressed_with_a_modifier_is_a_browser_shortcut_not_ours(
        self, served, modifier, key
    ):
        """Alt+Left is 'back', Ctrl+Home is 'top of page', Cmd+Arrow moves in text..."""
        got = load(served, f"press({json.dumps(key)}, 'Today', {{ {modifier}: true }});")
        assert got["controls"]["checked"] == ["Today"]
        assert got["controls"]["prevented"] == 0, "the shortcut must reach the browser"
        assert len(got["fetches"]) == 1

    @pytest.mark.parametrize("modifier", ["ctrlKey", "altKey", "metaKey", "shiftKey"])
    def test_the_same_key_without_the_modifier_still_switches(self, served, modifier):
        got = load(served, f"press('ArrowRight', 'Today', {{ {modifier}: false }});")
        assert got["controls"]["checked"] == ["Last session"]

    def test_keys_pressed_elsewhere_on_the_page_are_ignored(self, served):
        got = load(served, 'press("ArrowRight", null); press("Home", null);')
        assert got["controls"]["checked"] == ["Today"] and got["controls"]["prevented"] == 0

    def test_a_walk_round_the_whole_group_lands_where_it_started(self, served):
        got = load(
            served, 'for (const t of ["Today", "Last session", "All"]) press("ArrowRight", t);'
        )
        assert got["controls"]["checked"] == ["Today"]


# ══ remembered per browser, and fine without storage ══════════════════════════════════
@needs_node
class TestRemembered:
    @pytest.mark.parametrize(
        "text, view", [("Today", "today"), ("Last session", "last"), ("All", "all")]
    )
    def test_the_choice_survives_a_reload(self, served, text, view):
        first = load(served, f"click({json.dumps(text)});")
        stored = first["controls"]["stored"]
        assert stored, "nothing was remembered"
        again = load(served, storage=stored)
        assert again["controls"]["checked"] == [text]
        assert which_view(V.paper_html(again)) == view

    def test_loading_the_page_writes_nothing_to_storage(self, served):
        assert load(served)["controls"]["stored"] == {}, (
            "a browser that never chose remembers nothing"
        )

    def test_loading_leaves_a_remembered_choice_exactly_as_it_was(self, served):
        key = self.our_key(served)
        got = load(served, storage={key: '"all"'})
        assert got["controls"]["stored"] == {key: '"all"'}

    @pytest.mark.parametrize("raw", ['"bogus"', "{", "123"])
    def test_loading_does_not_rewrite_a_corrupt_value_either(self, served, raw):
        key = self.our_key(served)
        assert load(served, storage={key: raw})["controls"]["stored"] == {key: raw}

    def test_a_key_press_is_remembered_too(self, served):
        first = load(served, 'press("ArrowRight", "Today");')
        again = load(served, storage=first["controls"]["stored"])
        assert again["controls"]["checked"] == ["Last session"]

    def test_the_last_choice_wins(self, served):
        first = load(served, 'click("All"); click("Last session"); click("Today"); click("All");')
        again = load(served, storage=first["controls"]["stored"])
        assert again["controls"]["checked"] == ["All"]

    @staticmethod
    def our_key(served) -> str:
        """The localStorage key the page uses for the view: learned, never assumed."""
        (key,) = load(served, 'click("All");')["controls"]["stored"]
        return key

    @pytest.mark.parametrize(
        "raw",
        ['"bogus"', "{", "null", "123", '["last"]', '""', '"LAST"', "undefined", "true"],
        ids=["unknown", "broken-json", "null", "number", "list", "empty", "caps", "undef", "bool"],
    )
    def test_a_corrupt_stored_value_means_today(self, served, raw):
        got = load(served, storage={self.our_key(served): raw})
        assert got["controls"]["checked"] == ["Today"] and which_view(V.paper_html(got)) == "today"

    def test_a_stored_value_for_another_key_is_never_mistaken_for_ours(self, served):
        got = load(served, storage={"onelot.theme": '"night"', "other.view": '"all"'})
        assert got["controls"]["checked"] == ["Today"]


@needs_node
class TestWithoutStorage:
    """A private window or blocked site data: localStorage throws on every call."""

    def test_the_page_loads_on_today_and_the_panel_renders(self, served):
        got = load(served, throwing=True)
        assert got["controls"]["checked"] == ["Today"] and which_view(V.paper_html(got)) == "today"

    @pytest.mark.parametrize("text, view", [("Last session", "last"), ("All", "all")])
    def test_clicking_still_switches_the_view(self, served, text, view):
        got = load(served, f"click({json.dumps(text)});", throwing=True)
        assert got["controls"]["checked"] == [text] and which_view(V.paper_html(got)) == view
        assert got["controls"]["stored"] == {}

    def test_the_keyboard_still_works(self, served):
        got = load(
            served,
            'press("ArrowRight", "Today"); press("ArrowRight", "Last session");',
            throwing=True,
        )
        assert got["controls"]["checked"] == ["All"]

    def test_a_refresh_keeps_the_chosen_view_in_memory(self, served):
        got = load(served, 'click("All"); snapshot(); await advance(6000);', throwing=True)
        assert which_view(V.paper_html(got)) == "all"


# ══ any mix of clicks and keys ends where a simple model says ═════════════════════════
def model(ops: list[tuple[str, str | None, str | None]]) -> str:
    view = "Today"
    for kind, a, b in ops:
        if kind == "click":
            view = a  # type: ignore[assignment]
        elif kind == "key" and b is not None:
            i = TEXTS.index(b)
            view = {
                "ArrowRight": TEXTS[(i + 1) % 3],
                "ArrowDown": TEXTS[(i + 1) % 3],
                "ArrowLeft": TEXTS[(i + 2) % 3],
                "ArrowUp": TEXTS[(i + 2) % 3],
                "Home": TEXTS[0],
                "End": TEXTS[2],
            }.get(a or "", view)
    return view


@needs_node
@pytest.mark.parametrize("seed", range(8))
def test_random_clicks_and_keys_end_on_the_view_the_model_predicts(served, seed):
    rng = random.Random(seed)
    ops, js = [], []
    for _ in range(40):
        if rng.random() < 0.5:
            text = rng.choice(TEXTS)
            ops.append(("click", text, None))
            js.append(f"click({json.dumps(text)});")
        else:
            key = rng.choice(
                ["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End", "Tab"]
            )
            # a key is pressed on the focused button: the one the model says is current
            current = model(ops)
            ops.append(("key", key, current))
            js.append(f"press({json.dumps(key)}, {json.dumps(current)});")
    got = load(served, *js)
    want = model(ops)
    assert got["controls"]["checked"] == [want] and got["controls"]["tabbable"] == [want]
    assert (
        which_view(V.paper_html(got))
        == {"Today": "today", "Last session": "last", "All": "all"}[want]
    )
    assert len(got["fetches"]) == 1
