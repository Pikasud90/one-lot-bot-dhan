"""US-030 UX: the Paper status chips read at WCAG AA (>= 4.5:1) in both themes (developer test).

The pending and open chips used the desk's badge palette (--warn on --warn-bg, --cyan on
--cyan-bg): 3.7:1 and 3.6:1 in the Day theme. They now take their text from two stronger
variables, --warn-ink and --cyan-ink, defined in BOTH themes and used by the paper chips only;
the site-wide --warn and --cyan are untouched. Contrast is computed the way the chip is
painted: the ::before background composited over --panel, the text over that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLES = (Path(__file__).resolve().parents[2] / "src/onelot/ui/styles.css").read_text("utf-8")
NIGHT = ':root[data-theme="night"]'
AA = 4.5
# The desk-wide palette this change must leave alone (its own badges keep these).
SITE_WIDE = {
    ":root": {"--warn": "#b16900", "--cyan": "#0a8aa3"},
    NIGHT: {"--warn": "#ffb52e", "--cyan": "#2dd4ee"},
}


def rules() -> list[tuple[str, dict[str, str]]]:
    """(selector, declarations) of every top-level rule; comments dropped."""
    css = re.sub(r"/\*.*?\*/", "", STYLES, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = {}
        for decl in body.split(";"):
            prop, _, value = decl.partition(":")
            if value.strip():
                decls[prop.strip()] = value.strip()
        out.append((selector.strip(), decls))
    return out


def theme(night: bool) -> dict[str, str]:
    found = dict(rules())
    variables = dict(found[":root"])
    if night:
        variables |= found[NIGHT]
    return variables


def colour(value: str, variables: dict[str, str]) -> tuple[float, float, float, float]:
    value = value.strip()
    var = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if var:
        return colour(variables[var.group(1)], variables)
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), 1.0)
    parts = [float(p) for p in re.findall(r"[\d.]+", value)]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def over(top, bottom) -> tuple[float, float, float]:
    r, g, b, a = top
    return (a * r + (1 - a) * bottom[0], a * g + (1 - a) * bottom[1], a * b + (1 - a) * bottom[2])


def luminance(rgb) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def contrast(a, b) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def chip(name: str) -> tuple[str, str]:
    """(text colour, ::before background) of `.pchip.<name>`, the base rules merged in."""
    text, background = None, "transparent"
    for selector, decls in rules():
        for sel in (s.strip() for s in selector.split(",")):
            if sel in (".pchip", f".pchip.{name}"):
                text = decls.get("color", text)
            if sel in (".pchip::before", f".pchip.{name}::before"):
                background = decls.get("background", background)
    assert text, f".pchip.{name} has no text colour"
    return text, background


@pytest.mark.parametrize("night", [False, True], ids=["day", "night"])
@pytest.mark.parametrize("name", ["pending", "open", "nofill", "closed"])
def test_every_chip_reads_at_aa(name, night):
    variables = theme(night)
    panel = colour(variables["--panel"], variables)
    text, background = chip(name)
    bg = over(colour(background, variables), panel)
    ratio = contrast(over(colour(text, variables), bg), bg)
    assert ratio >= AA, f"{name} ({'night' if night else 'day'}): {ratio:.2f}:1"


@pytest.mark.parametrize(("name", "ink"), [("pending", "--warn-ink"), ("open", "--cyan-ink")])
def test_pending_and_open_take_their_text_from_the_ink_variables(name, ink):
    assert chip(name)[0] == f"var({ink})"


@pytest.mark.parametrize("selector", [":root", NIGHT])
def test_the_ink_variables_exist_in_both_themes(selector):
    declared = dict(rules())[selector]
    assert {"--warn-ink", "--cyan-ink"} <= set(declared), selector


@pytest.mark.parametrize("selector", [":root", NIGHT])
def test_the_site_wide_warn_and_cyan_are_untouched(selector):
    declared = dict(rules())[selector]
    for name, value in SITE_WIDE[selector].items():
        assert declared[name].lower() == value, (selector, name)


def test_only_the_paper_chips_use_the_ink_variables():
    users = [
        selector
        for selector, decls in rules()
        if any(re.search(r"var\(--(warn|cyan)-ink\)", v) for v in decls.values())
    ]
    assert users and all(s.startswith(".pchip.") for s in users), users
