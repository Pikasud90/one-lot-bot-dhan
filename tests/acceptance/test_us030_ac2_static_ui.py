"""US-030 AC2, the static facts: a Paper panel on the Desk page, CSS variables only.

AC2: "Given the Desk page, then a Paper panel lists open paper trades and today's closed ones
... it refreshes with the desk snapshot; Day theme default, large type, CSS variables only."
Manager's contract: both themes, mascots untouched, the panel fetches /api/paper on each desk
snapshot version change and no more often than every 5 s.

Pure file inspection (no browser, no node): index.html has a Paper container on the Live Desk
page and app.js feeds it from GET /api/paper; the new styles carry no hard-coded colour; the
rest of the page (theme default, type scale, mascots) is exactly as it was.

BASELINES below were measured on the story's kickoff commit (e25f0f6) with the counters in
us030_helpers. They are ceilings: a story that REMOVES a literal passes; one that adds fails.
"""

from __future__ import annotations

import re

import us030_helpers as H

BASELINE_STYLES_COLOUR_LITERALS = 52  # colour values in styles.css outside the two theme blocks
BASELINE_INDEX_INLINE_COLOUR_LITERALS = 0  # colours in style="" attributes, outside the mascots
MASCOT_SHA256 = "969c931aeeddb20c3ec689181ba086e8ad377cb925fdc173f250667d4493c357"
MIN_FONT_PX = 12  # the smallest type the desk already uses (the uppercase badges)

INDEX = H.INDEX_HTML.read_text(encoding="utf-8")
STYLES = H.STYLES_CSS.read_text(encoding="utf-8")
APP = H.APP_JS.read_text(encoding="utf-8")
JS_COLOUR = re.compile(r"(?<![\w-])#[0-9a-fA-F]{3,8}(?![\w-])|\b(?:rgba?|hsla?)\s*\(", re.I)


def paper_container_ids() -> list[str]:
    return re.findall(r'\bid="([^"]*paper[^"]*)"', H.live_section(INDEX), flags=re.I)


def paper_definitions() -> dict[str, str]:
    return {n: s for n, s in H.js_definitions().items() if "paper" in n.lower()}


def paper_rules() -> list[tuple[str, str]]:
    """The CSS rules the Paper panel brings: any selector naming paper, its chip or its container."""
    words = ["paper", "pchip", *paper_container_ids()]
    return [(s, b) for s, b in H.css_rules(STYLES) if any(w.lower() in s.lower() for w in words)]


def without_js_comments(src: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)


# ══ the harness itself (these pass before the story is built) ═════════════════════════
class TestHarness:
    def test_the_js_reader_finds_the_definitions_the_panel_will_sit_next_to(self):
        defs = H.js_definitions()
        for name in ("esc", "cls", "bd", "delayNote", "renderDesk", "applyStream"):
            assert name in defs, f"{name} not found among {len(defs)} top-level definitions"

    def test_the_css_reader_sees_both_theme_blocks_and_the_baseline_is_measured_the_same_way(self):
        selectors = [s for s, _ in H.css_rules(STYLES)]
        assert ":root" in selectors and ':root[data-theme="night"]' in selectors
        assert H.colour_literal_count_outside_themes(STYLES) <= BASELINE_STYLES_COLOUR_LITERALS


# ══ AC2: a Paper panel on the Desk page, fed by GET /api/paper ════════════════════════
class TestPanelContainer:
    def test_the_live_desk_page_has_a_paper_panel_container(self):
        assert paper_container_ids(), (
            'no element with an id containing "paper" inside <section data-tab="live">'
        )

    def test_the_panel_has_a_visible_paper_heading(self):
        assert re.search(r"<h[1-3][^>]*>\s*Paper\b", H.live_section(INDEX)), (
            "the Live Desk page has no heading that starts with 'Paper'"
        )

    def test_the_panel_is_on_the_desk_not_inside_the_legacy_engine_block(self):
        live = H.live_section(INDEX)
        legacy = re.search(r'<details class="card legacy".*?</details>', live, flags=re.S)
        ids = paper_container_ids()
        assert ids, "no Paper container"
        first = live.index(f'id="{ids[0]}"')
        assert not (legacy and legacy.start() <= first < legacy.end())

    def test_app_js_writes_into_that_container(self):
        ids = paper_container_ids()
        assert ids, "no Paper container"
        assert any(f'"{i}"' in APP or f"'{i}'" in APP or f"`{i}`" in APP for i in ids), (
            f"app.js never refers to {ids}"
        )


class TestFeedFromTheEndpoint:
    def test_app_js_reads_the_api_paper_route(self):
        assert "/api/paper" in APP

    def test_it_only_ever_reads_there_never_writes(self):
        for m in re.finditer(r"/api/paper", APP):
            call = APP[m.start() : m.start() + 120].split(")")[0]
            assert not re.search(r"POST|PUT|PATCH|DELETE|method\s*:|body\s*:", call, re.I), call

    def test_a_paper_function_is_called_from_the_desk_snapshot_handlers(self):
        names = list(paper_definitions())
        assert names, "app.js defines no paper* function"
        defs = H.js_definitions()
        hooks = defs.get("renderDesk", "") + defs.get("applyStream", "")
        assert any(re.search(rf"\b{re.escape(n)}\s*\(", hooks) for n in names), (
            f"none of {names} is called from renderDesk or applyStream: the panel would not "
            "refresh with the desk snapshot"
        )

    def test_the_refresh_is_rate_limited_to_one_fetch_per_5_seconds(self):
        """Heuristic: the paper code carries a 5000 ms (5 s) constant. Mode B verifies the
        behaviour in a browser."""
        source = "\n".join(without_js_comments(s) for s in paper_definitions().values())
        assert source, "app.js defines no paper* function"
        assert re.search(r"\b5_?000\b|\b5e3\b|\b5\s*\*\s*1000\b", source), (
            "no 5-second throttle constant in the paper code"
        )


# ══ AC2: CSS variables only, both themes, nothing else moved ══════════════════════════
class TestCssVariablesOnly:
    def test_the_papers_own_css_rules_use_no_literal_colour(self):
        offenders = {
            s: H.colour_literals_in_values(b)
            for s, b in paper_rules()
            if H.colour_literals_in_values(b)
        }
        assert not offenders, f"use var(--...) instead of literal colours: {offenders}"

    def test_styles_css_gains_no_hard_coded_colour_anywhere(self):
        count = H.colour_literal_count_outside_themes(STYLES)
        assert count <= BASELINE_STYLES_COLOUR_LITERALS, (
            f"{count} colour literals outside the theme blocks, the baseline is "
            f"{BASELINE_STYLES_COLOUR_LITERALS}: new colours belong in var(--...)"
        )

    def test_index_html_gains_no_inline_colour(self):
        count = H.html_colour_literals_outside_mascots(INDEX)
        assert count <= BASELINE_INDEX_INLINE_COLOUR_LITERALS, (
            f"{count} colour literals in style attributes"
        )

    def test_the_paper_javascript_hard_codes_no_colour(self):
        defs = paper_definitions()
        assert defs, "app.js defines no paper* function"
        for name, source in defs.items():
            found = JS_COLOUR.findall(without_js_comments(source))
            assert not found, f"{name} hard-codes a colour {found}: use the CSS classes/variables"

    def test_the_papers_own_type_stays_large(self):
        for selector, body in paper_rules():
            for px in re.findall(r"font-size\s*:\s*([\d.]+)px", body):
                assert float(px) >= MIN_FONT_PX, (
                    f"{selector}: font-size {px}px is below {MIN_FONT_PX}px"
                )


class TestNothingElseMoved:
    def test_the_mascots_are_untouched(self):
        assert H.mascot_digest(INDEX) == MASCOT_SHA256

    def test_the_day_theme_is_still_the_default(self):
        html_tag = re.search(r"<html[^>]*>", INDEX)
        assert html_tag and "data-theme" not in html_tag.group(0)
        root = dict(H.css_rules(STYLES))[":root"]
        assert "--bg:#eef1f7" in root.replace(" ", "") and "color-scheme:light" in root.replace(
            " ", ""
        )

    def test_the_night_toggle_and_theme_block_are_still_there(self):
        assert 'id="theme-toggle"' in INDEX
        assert ':root[data-theme="night"]' in [s for s, _ in H.css_rules(STYLES)]

    def test_the_type_scale_stays_large(self):
        bodies = [b for s, b in H.css_rules(STYLES) if s.replace(" ", "") == "html,body"]
        assert bodies and "font-size:17px" in bodies[0].replace(" ", "")
