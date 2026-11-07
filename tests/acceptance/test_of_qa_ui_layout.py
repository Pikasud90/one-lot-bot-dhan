"""Tester (QA): the page order the owner asked for (OF-1, OF-2), read from index.html.

The owner's words (2026-11-07):

* Home: "My portfolio on the top, then alerts section, then side by side: left Monitoring, right
  Strategy scoreboard ... the archive ... then the alerts below that, then ledger, journal,
  profile and everything else properly organised"; Monitoring must "scroll sideways".
* Live Desk: "Instrument focus shall go below market context, Indices and stocks and above bank
  nifty option engine".
"""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "src/onelot/ui/index.html").read_text(
    encoding="utf-8"
)


def section(name: str) -> str:
    m = re.search(rf'<section data-tab="{name}" class="tab">(.*?)</section>', HTML, re.S)
    assert m, f"no {name} section"
    return m.group(1)


def position(block: str, needle: str) -> int:
    at = block.find(needle)
    assert at >= 0, f"{needle!r} is not on the page"
    return at


def test_home_runs_portfolio_alerts_then_monitoring_beside_the_scoreboard_then_the_rest():
    home = section("home")
    order = [
        'id="acct-tiles"',  # My portfolio
        'id="home-signals"',  # Alerts
        'id="home-positions"',  # Monitoring (left)
        'id="home-scoreboard"',  # Strategy scoreboard (right)
        'id="home-archive"',
        'id="home-feed"',  # the alerts (feed) below the archive
        'id="acct-ledger"',
        'id="home-journal"',
        'id="acct-profile"',
        'id="acct-holdings"',
    ]
    at = [position(home, needle) for needle in order]
    assert at == sorted(at), dict(zip(order, at, strict=True))


def test_monitoring_and_the_scoreboard_share_one_row_monitoring_on_the_left():
    home = section("home")
    row = re.search(
        r'<div class="grid2">(?:(?!<div class="grid2">).)*?home-positions.*?home-scoreboard',
        home,
        re.S,
    )
    assert row, "Monitoring and the scoreboard are not side by side in one grid"
    assert 'id="home-archive"' not in row.group(0)
    assert "Monitoring" in row.group(0)


def test_monitoring_scrolls_sideways_so_every_column_can_be_reached():
    assert re.search(r'class="scroll" id="home-positions"', section("home"))
    css = (Path(__file__).resolve().parents[2] / "src/onelot/ui/styles.css").read_text(
        encoding="utf-8"
    )
    assert re.search(r"\.scroll\{[^}]*overflow-x:\s*auto", css)
    assert re.search(r"th,td\{[^}]*white-space:\s*nowrap", css), (
        "cells must not wrap, or the table shrinks"
    )


def test_the_archive_has_its_load_more_slot_right_under_it():
    home = section("home")
    archive = position(home, 'id="home-archive"')
    more = position(home, 'id="home-archive-more"')
    assert 0 < more - archive < 200
    assert "10 at a time" in home[archive - 400 : archive]


def test_live_desk_puts_instrument_focus_below_the_tables_and_above_the_bank_nifty_engine():
    live = section("live")
    order = [
        'id="desk-context"',  # Market context
        'id="desk-indices"',
        'id="desk-stocks"',
        'id="focus-card"',  # Instrument focus
        'class="card legacy"',  # Bank Nifty option engine
    ]
    at = [position(live, needle) for needle in order]
    assert at == sorted(at), dict(zip(order, at, strict=True))
