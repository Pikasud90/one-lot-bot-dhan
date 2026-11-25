"""US-085: the ergonomics pass. Static checks on the page: labelled controls, the guards, the tab
keys, and (25 Nov 2026) no text under 14px on the older tabs, from the shared stylesheet or an
inline style."""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "onelot" / "ui"
HTML = (UI / "index.html").read_text("utf-8")
APP = (UI / "app.js").read_text("utf-8")
SIM = (UI / "sim.js").read_text("utf-8")
CSS = (UI / "styles.css").read_text("utf-8")

# The owner's standing preference is large type: nothing on the page under 14px. sim.css and
# volume.css carry their own checks (tests/sim, tests/volume); these cover the shared stylesheet,
# the Machine Learning Lab's, and the inline styles of the page and the tab scripts.
FLOOR_PX = 14.0
FLOOR_STYLESHEETS = ("styles.css", "mlstates.css")
FLOOR_SCRIPTS = ("app.js", "charts.js", "mlstates.js", "volume.js", "sim_chart.js")
SMALL_KEYWORDS = {"xx-small", "x-small", "small", "smaller"}
SIZE_TOKEN = re.compile(
    r"(?<![\w.-])(max\([^)]*\)|min\([^)]*\)|clamp\([^)]*\)|var\([^)]*\)|[\d.]+(?:px|em|rem|%|pt)"
    r"|xx-small|x-small|smaller|small|medium|x-large|xx-large|larger|large)(?![\w-])"
)


def css_rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) of every rule, inside @media / @supports too; comments dropped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str]] = []

    def walk(text: str) -> None:
        i = 0
        while (j := text.find("{", i)) >= 0:
            selector, depth, k = text[i:j].strip(), 1, j + 1
            while k < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            body = text[j + 1 : k - 1]
            if selector.startswith(("@media", "@supports")):
                walk(body)
            elif not selector.startswith("@"):  # @keyframes and @font-face hold no text sizes
                rules.append((selector, body))
            i = k

    walk(css)
    return rules


def size_of(prop: str, value: str) -> str | None:
    """The size a `font-size` or `font` declaration sets, as written; None when it sets none."""
    value = value.replace("!important", "").strip().lower()
    if prop == "font-size":
        return value
    value = re.sub(r"/\s*[^\s,]+", " ", value)  # the shorthand's line-height is not a size
    found = SIZE_TOKEN.search(value)
    return found.group(1) if found else None


def under_floor(size: str) -> str | None:
    """Why `size` can render under 14px, or None when it cannot."""
    size = size.strip()
    fn = re.fullmatch(r"(max|min|clamp)\((.*)\)", size)
    if fn:
        args = [a.strip() for a in fn.group(2).split(",")]
        if fn.group(1) == "max":  # never below its largest fixed argument
            floors = [float(a[:-2]) for a in args if re.fullmatch(r"[\d.]+px", a)]
            return None if floors and max(floors) >= FLOOR_PX else f"{size} has no 14px floor"
        checks = args[:1] if fn.group(1) == "clamp" else args  # clamp(MIN, ...): the minimum
        reasons = [r for r in map(under_floor, checks) if r]
        return reasons[0] if reasons else None
    if size in SMALL_KEYWORDS:
        return f"{size} is under 14px"
    if size in {"inherit", "initial", "unset", "medium", "large", "larger", "x-large", "xx-large"}:
        return None
    number = re.fullmatch(r"([\d.]+)(px|em|rem|%|pt)", size)
    if not number:
        return f"{size} cannot be read here: write a px size, or max(14px, ...)"
    n, unit = float(number.group(1)), number.group(2)
    if unit == "px":
        return None if n >= FLOOR_PX else f"{size} is under 14px"
    if unit == "pt":
        return None if n * 4 / 3 >= FLOOR_PX else f"{size} is under 14px"
    if (unit == "%" and n >= 100) or (unit in ("em", "rem") and n >= 1):
        return None  # never smaller than its parent (or the root, 15.5px and up)
    return f"{size} shrinks with its parent (.85em was 13px on a phone): write max(14px,{size})"


def small_type_in_css(css: str) -> list[str]:
    offenders = []
    for selector, body in css_rules(css):
        for decl in body.split(";"):
            prop, _, value = decl.partition(":")
            prop = prop.strip().lower()
            if prop not in ("font", "font-size") or not value.strip():
                continue
            size = size_of(prop, value)
            why = under_floor(size) if size else None
            if why:
                offenders.append(f"{selector} {{{prop}:{value.strip()}}}: {why}")
    return offenders


def small_type_inline(source: str) -> list[str]:
    """font-size in style="" attributes or template strings, and a chart's fontSize option."""
    offenders = []
    for value in re.findall(r"font-size\s*:\s*([^;\"'`}]+)", source):
        why = under_floor(value)
        if why:
            offenders.append(f"font-size:{value.strip()}: {why}")
    for value in re.findall(r"\bfontSize\s*:\s*([\d.]+)", source):
        if float(value) < FLOOR_PX:
            offenders.append(f"fontSize: {value} (a chart's axis text) is under 14px")
    return offenders


def test_the_type_floor_check_catches_every_way_to_write_small_type() -> None:
    sample = """
      .a{font-size:13.5px} .b{font:700 12px/1 var(--ui)} .c{font-size:.85em}
      @media(max-width:700px){.d{font-size:smaller}} .e{font:11px/1.2 var(--mono)}
      .f{font-size:var(--small-type)} .g{font-size:max(12px,.9em)} .h{font-size:clamp(12px,2vw,18px)}
      .ok1{font-size:14px} .ok2{font:48px/1 var(--display)} .ok3{font:600 14px/1.35 var(--mono)}
      .ok4{font-size:max(14px,.85em)} .ok5{font:inherit;font-family:var(--mono)} .ok6{font-size:1.1em}
      .ok7{font:small-caps 700 15px/1 var(--ui)} @keyframes k{50%{opacity:.4}}
    """
    flagged = {line.split(" ", 1)[0] for line in small_type_in_css(sample)}
    assert flagged == {".a", ".b", ".c", ".d", ".e", ".f", ".g", ".h"}, flagged
    inline = (
        '<b style="font-size:12px">x</b><i style="font-size:.8em"></i><u style="font-size:16px">'
    )
    assert len(small_type_inline(inline + "fontSize: 12, fontSize: 14")) == 3


def test_the_shared_stylesheets_set_no_type_under_14px() -> None:
    for name in FLOOR_STYLESHEETS:
        offenders = small_type_in_css((UI / name).read_text("utf-8"))
        assert not offenders, f"{name}: type under {FLOOR_PX:g}px:\n" + "\n".join(offenders)


def test_no_inline_style_or_chart_sets_type_under_14px() -> None:
    sources = {"index.html": HTML} | {n: (UI / n).read_text("utf-8") for n in FLOOR_SCRIPTS}
    for name, source in sources.items():
        offenders = small_type_inline(source)
        assert not offenders, f"{name}: type under {FLOOR_PX:g}px:\n" + "\n".join(offenders)


def test_no_card_grid_is_wider_than_a_phone() -> None:
    """An auto-fit column over 300px wide is written min(Npx,100%): on a 390px phone a 400px
    card would otherwise push the whole page sideways, and the phone shrinks every letter."""
    for name in FLOOR_STYLESHEETS:
        for selector, body in css_rules((UI / name).read_text("utf-8")):
            for fixed in re.findall(r"repeat\(auto-(?:fit|fill),\s*minmax\(([^,]+),", body):
                px = re.fullmatch(r"([\d.]+)px", fixed.strip())
                assert not (px and float(px.group(1)) > 300), f"{name} {selector}: {fixed}"


def test_every_filter_control_has_a_label() -> None:
    for tag in re.findall(r"<(?:select|input)\b[^>]*>", HTML):
        if 'type="hidden"' in tag or 'type="checkbox"' in tag or 'type="radio"' in tag:
            continue
        ident = re.search(r'id="([\w-]+)"', tag)
        if ident and ident.group(1) in {
            "cov-q",
            "eda-series",
            "mllab-tf",
            "board-sort",
            "matrix-min",
        }:
            assert "aria-label=" in tag, ident.group(1)


def test_the_nine_tabs_and_the_keys_agree() -> None:
    nav = re.search(r'<nav class="tabs".*?</nav>', HTML, re.S)
    assert nav and len(re.findall(r"<a href=", nav.group(0))) == 9
    assert "Number keys 1 to 9" in APP


def test_the_clock_opens_the_calendar_from_the_keyboard() -> None:
    assert 'id="clockbox" role="button" tabindex="0"' in HTML
    assert '$("clockbox").addEventListener("keydown"' in APP


def test_a_repeated_change_request_joins_the_one_in_flight() -> None:
    assert "const inflight = new Map();" in APP and "inflight.has(key)" in APP


def test_the_simulator_guards_start_and_actions_and_confirms_ending() -> None:
    assert "if (S.starting) return;" in SIM and "S.acting" in SIM
    assert 'kind === "end"' in SIM and "confirm(" in SIM


def test_the_header_is_compact() -> None:
    assert "US-085: a compact header" in CSS and ".top.compact" in CSS
