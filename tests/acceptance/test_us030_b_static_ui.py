"""US-030 mode B: the Paper chips are legible in both themes, and every variable they use exists.

AC2 asks for "Day theme default, large type, CSS variables only, both themes". The mode-A tests
check that no colour is hard-coded; these resolve the chip colours THROUGH the variables, for
the Day theme (:root) and the Night theme (:root[data-theme="night"]), and compute the WCAG
contrast ratio of each chip's text on its own background over the panel.

The floor here is 3:1 (WCAG's minimum for UI components and large text), measured at the story's
kickoff palette. Two Day chips are below 4.5:1, the AA bar for small text, because they reuse
the desk's existing badge palette (--warn on --warn-bg, --cyan on --cyan-bg): pending 3.7:1
and open 3.6:1. That is recorded in the test report, not failed here: the existing badges have
the same ratios.
"""

from __future__ import annotations

import re

import pytest
import us030_helpers as H

STYLES = H.STYLES_CSS.read_text(encoding="utf-8")
CHIPS = ("pending", "open", "nofill", "closed")
MIN_CONTRAST = 3.0
NIGHT = ':root[data-theme="night"]'


def declarations(body: str) -> dict[str, str]:
    out = {}
    for decl in body.split(";"):
        prop, _, value = decl.partition(":")
        if value.strip():
            out[prop.strip().lower()] = value.strip()
    return out


def theme_variables(night: bool) -> dict[str, str]:
    rules = dict(H.css_rules(STYLES))
    variables = {k: v for k, v in declarations(rules[":root"]).items() if k.startswith("--")}
    if night:
        variables |= {k: v for k, v in declarations(rules[NIGHT]).items() if k.startswith("--")}
    return variables


def rgba(value: str, variables: dict[str, str]) -> tuple[float, float, float, float]:
    """A CSS colour (hex, rgb[a](), or var(--name)) as r, g, b, alpha."""
    value = value.strip()
    var = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if var:
        return rgba(variables[var.group(1)], variables)
    if value == "transparent":
        return (0, 0, 0, 0.0)
    hexed = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if hexed:
        h = hexed.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    func = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    assert func, f"cannot read the colour {value!r}"
    parts = [float(p) for p in re.split(r"[,\s/]+", func.group(1).strip())]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def over(top, bottom):
    r, g, b, a = top
    return tuple(a * t + (1 - a) * u for t, u in zip((r, g, b), bottom[:3], strict=True))


def luminance(rgb) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def chip_style(name: str) -> tuple[str, str]:
    """(text colour, background) declarations of `.pchip.<name>`, base rules merged in."""
    rules = H.css_rules(STYLES)
    merged: dict[str, str] = {}
    text = None
    for selector, body in rules:
        sel = selector.replace(" ", "")
        if sel in (".pchip", f".pchip.{name}"):
            text = declarations(body).get("color", text)
        if sel in (".pchip::before", f".pchip.{name}::before"):
            merged |= declarations(body)
    assert text, f".pchip.{name} sets no text colour"
    return text, merged.get("background", "transparent")


@pytest.mark.parametrize("night", [False, True], ids=["day", "night"])
class TestChipsInBothThemes:
    @pytest.mark.parametrize("chip", CHIPS)
    def test_the_chip_text_is_legible_on_its_background(self, chip, night):
        variables = theme_variables(night)
        panel = rgba(variables["--panel"], variables)[:3]
        text, background = chip_style(chip)
        bg = over(rgba(background, variables), panel)
        fg = over(rgba(text, variables), bg)
        ratio = contrast(fg, bg)
        assert ratio >= MIN_CONTRAST, (
            f"{chip} chip in the {'night' if night else 'day'} theme: {ratio:.2f}:1 "
            f"(text {text} on {background} over --panel)"
        )

    def test_the_four_chips_are_told_apart_by_colour(self, night):
        looks = {c: chip_style(c) for c in CHIPS}
        assert len(set(looks.values())) == 4, looks


class TestEveryVariableThePanelUsesIsDefined:
    def test_paper_rules_only_reference_variables_defined_in_both_themes(self):
        rules = H.css_rules(STYLES)
        ours = [
            body
            for selector, body in rules
            if any(w in selector.lower() for w in ("paper", "pchip"))
        ]
        assert ours, "no paper or chip CSS rules found"
        used = {v for body in ours for v in re.findall(r"var\((--[\w-]+)", body)}
        for night in (False, True):
            defined = theme_variables(night)
            missing = sorted(v for v in used if v not in defined)
            assert not missing, f"{'night' if night else 'day'}: undefined variables {missing}"

    def test_the_day_theme_is_the_unmarked_root_and_night_is_an_override(self):
        rules = dict(H.css_rules(STYLES))
        assert "--panel" in declarations(rules[":root"])
        assert "--panel" in declarations(rules[NIGHT])
