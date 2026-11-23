"""US-079 UI: the stop, target and trail rule in plain words on the page (developer tests).

    RATCHET_STAGES / RATCHET_REASONS   the stage and the two new close reasons in words
    levelsText(levels)                 "35% of the move locked · stop X (first Y) · target ..."
    paperReason(trade)                 a close reason in words; for an open trade its levels
    positionLevels(position)           the same beside a taken position's stop

The one-line `const name = ...;` helpers are lifted out of app.js and run in node, as
test_paper_panel_ui.py does; paperRow itself is run there with these lifted too.
"""

from __future__ import annotations

import re
import shutil

import pytest
from test_paper_panel_ui import closed, js, trade

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

LEVELS = {"stage": "LOCKED", "stop": "45315.00", "target": "45510.00",
          "first_stop": "45110.00", "first_target": "45510.00"}  # fmt: skip


def text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("BREAKEVEN_STOP", "stop above breakeven"),
        ("LOCK_STOP", "stop with 35% of the move locked"),
        ("TRAIL_STOP", "TRAIL STOP"),  # the reasons the page knew keep their words
        ("GAP_THROUGH_STOP", "GAP THROUGH STOP"),
    ],
)
def test_a_closed_trade_names_its_reason_in_words(reason: str, words: str) -> None:
    html = js("paperRow(P)", closed(close_reason=reason))
    assert f"<td>{words}</td>" in html


def test_an_open_trade_under_the_rule_shows_its_stage_and_levels() -> None:
    html = text(js("paperRow(P)", trade(levels=LEVELS)))
    assert "35% of the move locked · stop 45315.00 (first 45110.00) · target 45510.00 " \
           "(first 45510.00)" in html  # fmt: skip


def test_an_open_trade_without_the_rule_shows_a_dash() -> None:
    assert "<td>–</td>" in js("paperRow(P)", trade())


@pytest.mark.parametrize(
    ("stage", "words"),
    [("FIRST_STOP", "first stop"), ("BREAKEVEN", "stop above breakeven"),
     ("TRAILING", "trailing behind the best price")],
)  # fmt: skip
def test_every_stage_has_words(stage: str, words: str) -> None:
    assert js("levelsText(P)", LEVELS | {"stage": stage}).startswith(words + " · stop")


def test_nothing_from_the_api_reaches_the_page_unescaped() -> None:
    html = js("paperRow(P)", trade(levels=LEVELS | {"stop": "<b>x</b>"}))
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_a_taken_position_shows_its_stage_beside_the_stop() -> None:
    import test_paper_panel_ui as panel

    names = ["esc", "fmt", "RATCHET_STAGES", "levelsText", "positionLevels"]
    old = panel.HELPERS
    panel.HELPERS = names
    try:
        p = {"stage": "TRAILING", "stop_underlying": 45400, "initial_stop_underlying": 45110,
             "target_underlying": 45660, "first_target_underlying": 45510, "trail_active": True}  # fmt: skip
        html = js("positionLevels(P)", p)
        plain = js("positionLevels(P)", {"trail_active": True})
    finally:
        panel.HELPERS = old
    assert "trailing behind the best price · stop 45400.00 (first 45110.00)" in html
    assert "target 45660.00 (first 45510.00)" in html
    assert "trail" in plain and "stage" not in plain  # a position without the rule, as before
