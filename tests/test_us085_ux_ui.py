"""US-085: the ergonomics pass. Static checks on the page: labelled controls, the guards, the tab keys."""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "onelot" / "ui"
HTML = (UI / "index.html").read_text("utf-8")
APP = (UI / "app.js").read_text("utf-8")
SIM = (UI / "sim.js").read_text("utf-8")
CSS = (UI / "styles.css").read_text("utf-8")


def test_every_filter_control_has_a_label() -> None:
    for tag in re.findall(r"<(?:select|input)\b[^>]*>", HTML):
        if 'type="hidden"' in tag or 'type="checkbox"' in tag or 'type="radio"' in tag:
            continue
        ident = re.search(r'id="([\w-]+)"', tag)
        if ident and ident.group(1) in {"cov-q", "eda-series", "mllab-tf", "board-sort", "matrix-min"}:
            assert "aria-label=" in tag, ident.group(1)


def test_the_eight_tabs_and_the_keys_agree() -> None:
    nav = re.search(r'<nav class="tabs".*?</nav>', HTML, re.S)
    assert nav and len(re.findall(r"<a href=", nav.group(0))) == 8
    assert "Number keys 1 to 8" in APP


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
